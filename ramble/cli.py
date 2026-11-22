"""`ramble` command line. Run `ramble -h`."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import subprocess
import sys
import time

from . import config, db, state, voice_control


def parse_duration(s: str) -> float:
    m = re.fullmatch(r"(\d+(?:\.\d+)?)([smh]?)", s.strip())
    if not m:
        raise argparse.ArgumentTypeError("use e.g. 90m, 2h, 30s")
    return float(m.group(1)) * {"s": 1, "m": 60, "h": 3600, "": 60}[m.group(2)]


def fmt(ts: float | None) -> str:
    return time.strftime("%m-%d %H:%M:%S", time.localtime(ts)) if ts else "-"


def cmd_on(a) -> None:
    state.write(True)
    print("ramble: on")


def cmd_off(a) -> None:
    if a.duration:
        s = state.write(True, time.time() + a.duration)
    else:
        s = state.write(False)
    print("ramble:", state.describe(s))


def cmd_toggle(a) -> None:
    s = state.write(not state.is_listening())
    print("ramble:", state.describe(s))


def cmd_status(a) -> None:
    conn = db.connect()
    print(f"state: {state.describe()}")
    print(f"voice responses: {'on' if voice_control.read()['enabled'] else 'off'}")
    running = subprocess.run(["pgrep", "-f", "ramble.(app|cli run)"], capture_output=True, text=True).stdout.split()
    print(f"app process: {'running (pid ' + ', '.join(running) + ')' if running else 'NOT running (ramble install / ramble run)'}")
    today = time.mktime(time.strptime(time.strftime("%Y-%m-%d"), "%Y-%m-%d"))
    for label, sql, args in [
        ("segments today", "SELECT status, COUNT(*), ROUND(SUM(duration)/60.0,1) FROM segments WHERE started_at>=? GROUP BY status", (today,)),
        ("thoughts today", "SELECT status || ':' || COALESCE(classification,'-'), COUNT(*), '' FROM thoughts WHERE started_at>=? GROUP BY 1", (today,)),
        ("dispatches today", "SELECT status, COUNT(*), '' FROM dispatches WHERE created_at>=? GROUP BY status", (today,)),
    ]:
        rows = conn.execute(sql, args).fetchall()
        print(f"{label}: " + (", ".join(f"{r[0]}={r[1]}" + (f" ({r[2]} min)" if r[2] not in ("", None) else "") for r in rows) or "none"))
    last = conn.execute("SELECT started_at, text FROM segments WHERE status='transcribed' ORDER BY started_at DESC LIMIT 1").fetchone()
    if last:
        print(f"last heard: {fmt(last[0])} {last[1][:100]}")
    print(f"data: {config.HOME}")


def cmd_voice(a) -> None:
    saved = voice_control.read() if a.setting == "status" else voice_control.write(a.setting == "on")
    print(f"ramble: voice responses {'on' if saved['enabled'] else 'off'}; mic {state.describe()}")


def cmd_today(a) -> None:
    path = config.DIGEST_DIR / f"{a.date or time.strftime('%Y-%m-%d')}.md"
    print(path.read_text() if path.exists() else f"no digest yet at {path}")


def cmd_thoughts(a) -> None:
    conn = db.connect()
    for r in conn.execute("SELECT * FROM thoughts ORDER BY started_at DESC LIMIT ?", (a.n,)).fetchall()[::-1]:
        print(f"{r['id']}  {fmt(r['started_at'])}  {r['status']:9} {r['classification'] or '-':10} {(r['summary'] or r['text'].splitlines()[-1])[:90]}")


def cmd_show(a) -> None:
    conn = db.connect()
    t = conn.execute("SELECT * FROM thoughts WHERE id LIKE ?", (f"%{a.id}%",)).fetchone()
    if not t:
        sys.exit("no such thought")
    print(f"# {t['id']} {t['status']} {t['classification']}\n\n{t['text']}\n")
    if t["triage_json"]:
        print(json.dumps(json.loads(t["triage_json"]), indent=2))
    if t["error"]:
        print("error:", t["error"])
    for d in conn.execute("SELECT * FROM dispatches WHERE thought_id=?", (t["id"],)):
        print(f"dispatch {d['name']} [{d['status']}] cwd={d['cwd']} thread={d['thread_id']}")


def cmd_retriage(a) -> None:
    conn = db.connect()
    n = conn.execute("UPDATE thoughts SET status='closed', error=NULL WHERE id LIKE ?", (f"%{a.id}%",)).rowcount
    print(f"requeued {n} thought(s); the running app will triage them")


def cmd_tail(a) -> None:
    conn = db.connect()
    seen = set()
    since = time.time() - a.minutes * 60
    while True:
        rows = conn.execute(
            "SELECT id, started_at, status, text FROM segments WHERE started_at>=? AND status IN ('transcribed','empty') ORDER BY started_at",
            (since,),
        ).fetchall()
        for r in rows:
            if r["id"] not in seen:
                seen.add(r["id"])
                mark = "" if r["status"] == "transcribed" else "  (dropped)"
                print(f"{fmt(r['started_at'])}  {r['text']}{mark}", flush=True)
        time.sleep(1)


def cmd_ingest(a) -> None:
    from .recorder import ingest_file, ingest_companion

    ids = [ingest_companion(a.path)] if a.companion else ingest_file(a.path, config.load())
    print(f"queued {len(ids)} segment(s) for the running app: {', '.join(ids)}")


def cmd_say(a) -> None:
    from .speech import speak, synthesize

    try:
        fn = synthesize if a.no_play else speak
        result = fn(a.text, voice=a.voice, output=a.output)
    except Exception as exc:
        sys.exit(f"ramble: speech failed: {exc}")
    print(f"{'generated' if a.no_play else 'spoke'} via {result.engine}: {result.path} ({result.duration:.2f}s)")


def cmd_reading(a) -> None:
    """Control the installed reader without launching a second playback process."""
    from .reading import ReadingStore
    from . import reading_interaction

    store = ReadingStore()
    try:
        if a.operation == "start":
            if a.file:
                path = Path(a.file).expanduser()
                if path.stat().st_size > 1_000_000:
                    raise ValueError("The article file is too large; use at most 1 MB of text.")
                content = path.read_text()
            elif a.stdin:
                content = sys.stdin.read(1_000_001)
                if len(content) > 1_000_000:
                    raise ValueError("The article is too large; use at most 1 MB of text.")
            else:
                content = a.text
            result = store.start(content, title=a.title or "Article", source_url=a.source_url,
                                 request_id=a.request_id)
        elif a.operation in {"ask", "recap"}:
            result = reading_interaction.submit_question(getattr(a, "message", ""),
                        recap=a.operation == "recap", reading_id=a.reading_id, request_id=a.request_id)
        elif a.operation == "status":
            result = store.snapshot()
            result["discussion"] = reading_interaction.snapshot()
        else:
            result = store.control(a.operation, reading_id=a.reading_id,
                                   passage_index=getattr(a, "passage", None), request_id=a.request_id)
        if a.operation != "status" and isinstance(result.get("reading"), dict):
            row = result["reading"]
            result = {"reading_id": row["id"], "title": row["title"], "status": row["status"],
                      "position": row["position"], "passages": len(row["passages"]),
                      "controller_online": result["controller_online"]}
        print(json.dumps(result, ensure_ascii=False))
    except (ValueError, OSError) as exc:
        sys.exit(f"ramble: {exc}")


def cmd_inject(a) -> None:
    """Create a closed thought from text, skipping audio (tests triage + dispatch)."""
    conn = db.connect()
    tid = db.new_id()
    now = time.time()
    conn.execute(
        "INSERT INTO thoughts (id, started_at, ended_at, status, text) VALUES (?,?,?,'closed',?)",
        (tid, now, now, f"[{time.strftime('%H:%M:%S')}] {a.text}"),
    )
    print(f"injected thought {tid}; the running app will triage it")


def cmd_transcript_note(a) -> None:
    """Build one day's owner-speech note; writes files only unless --deliver is given."""
    import datetime as dt
    from zoneinfo import ZoneInfo
    from .source_gate import SourceGate
    from . import transcript_route
    cfg = config.load()
    day = (dt.date.fromisoformat(a.date) if a.date
           else dt.datetime.now(ZoneInfo(cfg.get("timezone") or "UTC")).date() - dt.timedelta(days=1))
    conn = db.connect()
    note = transcript_route.build_day(conn, day, cfg, SourceGate(cfg))
    result = transcript_route.deliver(note, cfg, live=a.deliver)
    print(json.dumps({"title": note["title"], **note["stats"], **result}, indent=1))


def cmd_owner_voice(a) -> None:
    """Enroll the owner voice from shortcut dictation (always the owner speaking) or show the profile."""
    from . import speaker_embedding as se
    path = config.HOME / config.load().get("source_gate_profile", "owner-voice.json")
    if a.operation == "status":
        if not path.exists():
            sys.exit(f"no owner-voice profile at {path}; run: ramble owner-voice enroll")
        prof = se.OwnerProfile.load(path)
        print(json.dumps({"path": str(path), "threshold": prof.threshold, **prof.meta}, indent=1))
        return
    model = se.default_model_path()
    if model is None:
        sys.exit("WeSpeaker model not cached; set RAMBLE_OWNER_VOICE_MODEL to voxceleb_resnet34_LM.onnx")
    from .source_gate import _read_audio
    conn = db.connect()
    rows = conn.execute("SELECT audio_path FROM segments WHERE source LIKE 'dictation:%' AND status='transcribed' "
                        "AND duration >= 1.0 ORDER BY started_at DESC LIMIT ?", (a.limit,)).fetchall()
    embedder = se.SpeakerEmbedder(model)
    vectors = [embedder.embed(_read_audio(r[0])) for r in rows if Path(r[0]).exists()]
    prof = se.OwnerProfile.enroll(vectors, a.threshold, {"source": "dictation segments", "model": str(model)})
    prof.save(path)
    print(f"enrolled {len(vectors)} dictation clips; threshold {a.threshold}; saved {path}")


def cmd_output_activity(a) -> None:
    from .output_activity import CoreAudioReader, ramble_pids
    pids = CoreAudioReader().output_pids() - ramble_pids()
    print(json.dumps({"external_output_active": bool(pids), "pids": sorted(pids)}))


def cmd_run(a) -> None:
    from .daemon import Daemon, setup_logging

    setup_logging()
    Daemon().run_headless()


def cmd_install(a) -> None:
    subprocess.run([str(config.REPO / "bin" / "install")], check=True)


def cmd_uninstall(a) -> None:
    subprocess.run([str(config.REPO / "bin" / "uninstall")], check=True)


def main() -> None:
    p = argparse.ArgumentParser(prog="ramble", description="Ambient voice capture -> transcription -> triage -> agents.")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("on", help="start listening").set_defaults(fn=cmd_on)
    off = sub.add_parser("off", help="stop listening (optionally for a while: ramble off 2h)")
    off.add_argument("duration", nargs="?", type=parse_duration)
    off.set_defaults(fn=cmd_off)
    sub.add_parser("toggle", help="flip on/off").set_defaults(fn=cmd_toggle)
    sub.add_parser("status", help="state and today's counts").set_defaults(fn=cmd_status)
    voice = sub.add_parser("voice", help="control voice responses independently of transcription")
    voice.add_argument("setting", choices=("on", "off", "status"), nargs="?", default="status")
    voice.set_defaults(fn=cmd_voice)
    t = sub.add_parser("today", help="print today's digest")
    t.add_argument("date", nargs="?", help="YYYY-MM-DD")
    t.set_defaults(fn=cmd_today)
    th = sub.add_parser("thoughts", help="list recent thoughts")
    th.add_argument("-n", type=int, default=20)
    th.set_defaults(fn=cmd_thoughts)
    sh = sub.add_parser("show", help="show a thought, its triage decision and dispatches")
    sh.add_argument("id")
    sh.set_defaults(fn=cmd_show)
    rt = sub.add_parser("retriage", help="re-run triage for a thought")
    rt.add_argument("id")
    rt.set_defaults(fn=cmd_retriage)
    tl = sub.add_parser("tail", help="follow live transcripts")
    tl.add_argument("--minutes", type=float, default=10)
    tl.set_defaults(fn=cmd_tail)
    ig = sub.add_parser("ingest", help="push an audio file through VAD + transcription")
    ig.add_argument("path")
    ig.add_argument("--companion", action="store_true", help="queue a deliberate Companion voice request")
    ig.set_defaults(fn=cmd_ingest)
    sy = sub.add_parser("say", help="speak via the configured TTS provider (macOS fallback), protected from mic feedback")
    sy.add_argument("text")
    sy.add_argument("--voice", help="Kokoro voice name or ElevenLabs voice ID (default: provider config)")
    sy.add_argument("--output", help="save generated WAV at this path")
    sy.add_argument("--no-play", action="store_true", help="only generate the WAV")
    sy.set_defaults(fn=cmd_say)
    reading = sub.add_parser("reading", help="persistent article reading and read-along")
    operations = reading.add_subparsers(dest="operation", required=True)
    start = operations.add_parser("start", help="read exact article text through the installed app")
    source = start.add_mutually_exclusive_group(required=True)
    source.add_argument("--file", help="UTF-8 text file containing the article")
    source.add_argument("--stdin", action="store_true", help="read article text from stdin")
    source.add_argument("--text", help="article text")
    start.add_argument("--title")
    start.add_argument("--source-url", help="original source URL, retained as metadata")
    start.add_argument("--request-id", help="stable ID for safe retry")
    start.set_defaults(fn=cmd_reading)
    for operation in ("status", "pause", "resume", "seek", "recap", "ask"):
        command = operations.add_parser(operation)
        command.add_argument("--reading-id", help="reject changes if another article is selected")
        command.add_argument("--request-id", help="stable ID for safe retry")
        if operation == "seek":
            command.add_argument("passage", type=int, help="zero-based passage index; leaves playback paused")
        if operation == "ask":
            command.add_argument("message")
        command.set_defaults(fn=cmd_reading)
    ij = sub.add_parser("inject", help="test: create a thought from text (skips audio)")
    ij.add_argument("text")
    ij.set_defaults(fn=cmd_inject)
    tn = sub.add_parser("transcript-note", help="build a day's owner-speech note (files only unless --deliver)")
    tn.add_argument("--date", help="local date YYYY-MM-DD; default yesterday")
    tn.add_argument("--deliver", action="store_true", help="also run transcript_route.deliver_command")
    tn.set_defaults(fn=cmd_transcript_note)
    ov = sub.add_parser("owner-voice", help="enroll or inspect the owner-voice profile used by the source gate")
    ov.add_argument("operation", choices=["enroll", "status"])
    ov.add_argument("--threshold", type=float, default=0.20,
                    help="cosine threshold (0.20 kept 95%% of held-out owner clips in the Nov 2026 evaluation)")
    ov.add_argument("--limit", type=int, default=300, help="most recent dictation clips to enroll")
    ov.set_defaults(fn=cmd_owner_voice)
    sub.add_parser("output-activity", help="show whether another process is playing audio now (read-only)"
                   ).set_defaults(fn=cmd_output_activity)
    sub.add_parser("run", help="run the daemon in the foreground without the menu bar").set_defaults(fn=cmd_run)
    sub.add_parser("install", help="build Ramble.app and start it at login").set_defaults(fn=cmd_install)
    sub.add_parser("uninstall", help="stop and remove the login item").set_defaults(fn=cmd_uninstall)
    a = p.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
