"""Shared model preferences and provider discovery for the Mac and Companion."""
from __future__ import annotations

from contextlib import contextmanager
import fcntl
import logging
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
import time

from . import config

log = logging.getLogger("ramble.models")
LANES = {
    "fast": ("Quick voice replies", "fast_model", "fast_effort"),
    "triage": ("Thought review", "triage_model", "triage_effort"),
    "work": ("New work sessions", "work_session_model", "work_session_effort"),
}
WORKLOADS = ("conversation", "work")
_catalog_lock = threading.Lock()
_cached_catalog = (0, None)
_last_catalog = None  # last successful discovery, retained for non-blocking validation
_limits_lock = threading.Lock()
_cached_limits = (0, None)


class ConflictError(ValueError):
    pass


def _read(home=None):
    target = (home if home is not None else config.HOME) / "config.json"
    try:
        raw = target.read_bytes()
    except FileNotFoundError:
        raw = b""
    overrides = json.loads(raw) if raw else {}
    if not isinstance(overrides, dict):
        raise ValueError("Ramble config.json must contain an object.")
    return target, overrides, hashlib.sha256(raw).hexdigest()


def selection(cfg, lane):
    """Read only model overrides at a request boundary; keep an in-flight pair stable."""
    _, overrides, _ = _read()
    _, model_key, effort_key = LANES[lane]
    return {"model": overrides.get(model_key, cfg.get(model_key)),
            "effort": overrides.get(effort_key, cfg.get(effort_key))}


def fast_timeout(cfg, selected):
    """Budget the selected reasoning level without silently changing the model.

    The wait notice is independent of this hard deadline. An explicit numeric
    timeout remains authoritative, including for high-effort models.
    """
    override = cfg.get("fast_timeout_s")
    if override is not None:
        if isinstance(override, bool) or not isinstance(override, (int, float)) or not math.isfinite(override) or override <= 0:
            raise ValueError("fast_timeout_s must be null or a positive finite number")
        return override
    return {"none": 30, "minimal": 30, "low": 30, "medium": 60,
            "high": 90, "xhigh": 120, "max": 120, "ultra": 900}.get(selected["effort"], 120)


def work_selection(cfg, *, model=None, effort=None):
    """Resolve a per-session request without changing saved defaults.

    An explicitly chosen model with no effort inherits that model's provider
    default, rather than an effort saved for a different model. Effort alone
    applies to the saved work model, or the provider's default model.
    """
    selected = selection(cfg, "work")
    if model is None and effort is None:
        return selected
    models = catalog()
    if model is not None:
        selected = {"model": model, "effort": effort}
    else:
        selected["effort"] = effort
    chosen = next((m for m in models if m["id"] == selected["model"]), None)
    if selected["model"] is None:
        chosen = next((m for m in models if m["is_default"]), None)
        # Pin the provider default too, so queued commands survive changes.
        if chosen:
            selected["model"] = chosen["id"]
    if chosen is None:
        raise ValueError("That work model is unavailable. Choose an available model.")
    if selected["effort"] is not None and selected["effort"] not in chosen["efforts"]:
        raise ValueError("That reasoning level is not supported by the selected model.")
    return selected


def _override_pair(cfg, model_key, effort_key):
    _, overrides, _ = _read()
    return overrides.get(model_key, cfg.get(model_key)), overrides.get(effort_key, cfg.get(effort_key))


def known_catalog():
    """Return the last discovered catalog without blocking (None before discovery)."""
    return _cached_catalog[1] if _cached_catalog[1] is not None else _last_catalog


def run_discovery(stop, interval_s=60):
    """Daemon-owned refresh, so the voice path validates without waiting on discovery."""
    while not stop.is_set():
        for refresh in (catalog, provider_limits):
            try:
                refresh()
            except OSError as exc:
                log.info("model discovery: %s", exc)
        stop.wait(interval_s)


def _validate_profile(pair, models, fallback_pair):
    """Validate a policy-chosen pair. Policy choices may fall back; explicit ones never do."""
    if models is None:
        return pair, False, None
    by_id = {m["id"]: m for m in models}
    if pair["model"] is None:
        return pair, True, None
    chosen = by_id.get(pair["model"])
    if chosen is None:
        if fallback_pair is not None:
            return fallback_pair, True, f"model {pair['model']} is not advertised; used the saved work pair"
        return pair, False, f"model {pair['model']} is not advertised"
    if pair["effort"] is not None and pair["effort"] not in chosen["efforts"]:
        return ({"model": pair["model"], "effort": chosen.get("default_effort")}, True,
                f"effort {pair['effort']} is unsupported by {pair['model']}; used its default")
    return pair, True, None


def resolve_work(cfg, *, workload=None, model=None, effort=None):
    """Choose a work-session pair as an explicit, recorded routing decision.

    Explicit user choices are validated against live discovery and never
    substituted. Otherwise the router's workload selects a profile: conversation
    uses the low-latency conversation pair (default: the quick-reply pair), and
    work uses the saved work-session defaults. Validation uses the last known
    catalog so discovery never blocks the voice path.
    """
    requested = {"model": model, "effort": effort}
    if model is not None or effort is not None:
        selected = work_selection(cfg, model=model, effort=effort)
        return {**selected, "basis": "explicit", "workload": workload, "requested": requested,
                "validated": True, "fallback": None}
    saved = selection(cfg, "work")
    if workload == "conversation":
        conv_model, conv_effort = _override_pair(cfg, "work_conversation_model", "work_conversation_effort")
        if conv_model is None and conv_effort is None:
            fast = selection(cfg, "fast")
            conv_model, conv_effort = fast["model"], fast["effort"]
        pair, validated, fallback = _validate_profile(
            {"model": conv_model, "effort": conv_effort}, known_catalog(), saved)
        basis = "work" if pair is saved else "conversation"
    else:
        pair, validated, fallback = _validate_profile(saved, known_catalog(), None)
        basis = "work"
    return {**pair, "basis": basis, "workload": workload, "requested": requested,
            "validated": validated, "fallback": fallback}


def followup_selection(cfg):
    """Admission is a small classification: quick-reply model at a low effort."""
    fast = selection(cfg, "fast")
    _, overrides, _ = _read()
    effort = overrides.get("work_followup_effort", cfg.get("work_followup_effort")) or fast["effort"]
    models = known_catalog()
    chosen = next((m for m in models or [] if m["id"] == fast["model"]), None)
    if chosen is not None and effort not in chosen["efforts"]:
        effort = fast["effort"]
    return {"model": fast["model"], "effort": effort}


def provider_limits(client="codex", home=None):
    """Read reported quota windows through the running app (bounded, never a model turn)."""
    global _cached_limits
    with _limits_lock:
        stamp, value = _cached_limits
        if value is not None and time.monotonic() - stamp < 120:
            return value
        launcher = Path.home() / '.local/bin/agent-coord'
        cli = os.environ.get('AGENT_COORD_CLI') or (str(launcher) if launcher.is_file() else shutil.which('agent-coord'))
        if not cli:
            raise OSError("Agent Coord launcher is unavailable.")
        receipt, result = {}, None
        try:
            caller = os.environ.get('AGENT_COORD_SESSION_ID') or os.environ.get('CODEX_THREAD_ID')
            if not caller:
                caller = ((home if home is not None else config.HOME) / 'agent_coord_session').read_text().strip()
            result = subprocess.run(
                [cli, "thread", "provider-limits", "--client", client, "--from-session", caller, "--wait", "5"],
                capture_output=True, text=True, timeout=10,
            )
            receipt = json.loads(result.stdout) if result.returncode == 0 else {}
            provider = receipt["result"]["provider"]
        except subprocess.TimeoutExpired as exc:
            raise OSError("Provider limits are unavailable: discovery timed out.") from exc
        except (ValueError, KeyError, TypeError) as exc:
            detail = (receipt.get("error") if isinstance(receipt, dict) else None) or (
                (result.stderr or result.stdout or "").strip()[-200:] if result else "")
            raise OSError(f"Provider limits are unavailable: {detail or 'no result'}") from exc
        _cached_limits = (time.monotonic(), provider)
        return provider


def known_limits():
    """Return cached limits without blocking; stale beyond ten minutes counts as unknown."""
    stamp, value = _cached_limits
    return value if value is not None and time.monotonic() - stamp < 600 else None


def exhausted_window(provider, now=None):
    """Return the exhausted reported window, if any. Shared pools are not per-model proof."""
    now = time.time() if now is None else now
    for window in (provider or {}).get("windows", []):
        used = window.get("used_percent")
        resets = window.get("resets_at")
        if isinstance(used, (int, float)) and used >= 100 and (not resets or resets > now):
            return window
    return None


def work_routing_context(cfg):
    """Supply live choices for a routing request without starting a model turn."""
    defaults = selection(cfg, "work")
    try:
        models = catalog()
    except OSError:
        return "Work model discovery unavailable. Clarify model-specific requests; do not silently use another model."
    return "Work model choices (exact IDs and supported efforts): " + json.dumps(
        {"defaults": defaults, "models": models}, ensure_ascii=False
    )


def catalog(home=None):
    """Discover models through the owning app, without starting any agent turn."""
    global _cached_catalog
    with _catalog_lock:
        now = time.monotonic()
        if _cached_catalog[1] is not None and now - _cached_catalog[0] < 60:
            return _cached_catalog[1]
        try:
            launcher = Path.home() / '.local/bin/agent-coord'
            cli = os.environ.get('AGENT_COORD_CLI') or (str(launcher) if launcher.is_file() else shutil.which('agent-coord'))
            if not cli:
                raise OSError("Agent Coord launcher is unavailable.")
            # App services have no interactive caller identity. Reuse Ramble's
            # existing dispatcher registration; discovery must never create an agent.
            args = []
            caller = os.environ.get('AGENT_COORD_SESSION_ID') or os.environ.get('CODEX_THREAD_ID')
            if not caller:
                caller = ((home if home is not None else config.HOME) / 'agent_coord_session').read_text().strip()
            if caller:
                args = ['--from-session', caller]
            result = subprocess.run(
                [cli, "thread", "models", "--client", "codex", "--cwd", str(config.REPO),
                 *args, "--wait", "10"], capture_output=True, text=True, timeout=15,
            )
            receipt = json.loads(result.stdout) if result.returncode == 0 else {}
            if receipt.get("status") != "completed":
                raise ValueError("Model discovery did not complete.")
            models = []
            for row in receipt["result"]["models"]:
                if row.get("hidden"):
                    continue
                models.append({"id": row.get("model") or row["id"],
                               "label": row.get("displayName") or row["id"],
                               "efforts": [e["reasoningEffort"] for e in row.get("supportedReasoningEfforts", [])],
                               "default_effort": row.get("defaultReasoningEffort"),
                               "is_default": row.get("isDefault", False)})
            if not models:
                raise ValueError("No models returned.")
        except (OSError, ValueError, KeyError, TypeError, subprocess.TimeoutExpired) as exc:
            raise OSError("Models are unavailable. Check Ribbon Field on the Mac, then reload settings.") from exc
        global _last_catalog
        _cached_catalog = (time.monotonic(), models)
        _last_catalog = models
        return models


def _snapshot(overrides, version):
    cfg = {**config.DEFAULTS, **overrides}
    return {"version": version, "triage_backend": cfg["triage_backend"],
            "settings": {lane: {"model": cfg[mk], "effort": cfg[ek]}
                         for lane, (_, mk, ek) in LANES.items()}}


def snapshot(home=None):
    _, overrides, version = _read(home)
    return {**_snapshot(overrides, version), "models": catalog(home)}


@contextmanager
def _write_lock(target):
    target.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(target.parent / ".model-settings.lock", os.O_CREAT | os.O_RDWR, 0o600)
    with os.fdopen(fd, "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def save(settings, *, version, home=None):
    if not isinstance(version, str) or not isinstance(settings, dict) or set(settings) != set(LANES):
        raise ValueError("Load all model settings before saving.")
    models = catalog(home)
    by_id = {m["id"]: m for m in models}
    target, _, _ = _read(home)
    with _write_lock(target):
        _, overrides, current_version = _read(home)
        if version != current_version:
            raise ConflictError("Settings changed elsewhere. Reload saved settings before saving again.")
        current = _snapshot(overrides, current_version)
        for lane, (_, mk, ek) in LANES.items():
            pair = settings[lane]
            if not isinstance(pair, dict) or set(pair) != {"model", "effort"}:
                raise ValueError("Each setting needs a model and reasoning level.")
            # Preserve legacy/custom settings unless the user changes that lane.
            if pair == current["settings"][lane]:
                continue
            if lane == "triage" and current["triage_backend"] != "codex":
                raise ValueError("Thought review uses Claude. Its settings must be changed on the Mac in config.json.")
            model, effort = pair["model"], pair["effort"]
            if model is None and lane == "work":
                if effort is not None:
                    raise ValueError("Choose a work model before choosing its reasoning level.")
            else:
                if not isinstance(model, str) or model not in by_id:
                    raise ValueError("Choose an available model.")
                if not (effort is None and lane == "work") and effort not in by_id[model]["efforts"]:
                    raise ValueError("Choose a reasoning level supported by the selected model.")
            overrides.update({mk: model, ek: effort})
        raw = (json.dumps(overrides, indent=2, ensure_ascii=False) + "\n").encode()
        fd, temporary = tempfile.mkstemp(prefix=".model-settings-", dir=target.parent)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, target)
        finally:
            Path(temporary).unlink(missing_ok=True)
        return {**_snapshot(overrides, hashlib.sha256(raw).hexdigest()), "models": models}
