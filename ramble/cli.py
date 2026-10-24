"""`ramble` command line. Run `ramble -h`."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import tempfile
import time

from . import config, db, state


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
    from .recorder import ingest_file

    ids = ingest_file(a.path, config.load())
    print(f"queued {len(ids)} segment(s) for the running app: {', '.join(ids)}")


def cmd_say(a) -> None:
    """Speak text with macOS `say` and push it through the full audio pipeline (testing)."""
    from .recorder import ingest_file

    with tempfile.TemporaryDirectory() as d:
        path = f"{d}/say.aiff"
        subprocess.run(["say", "-o", path, a.text], check=True)
        ids = ingest_file(path, config.load())
    print(f"queued {len(ids)} segment(s): {', '.join(ids)}")


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
    ig.set_defaults(fn=cmd_ingest)
    sy = sub.add_parser("say", help="test: synthesize speech with `say` and ingest it")
    sy.add_argument("text")
    sy.set_defaults(fn=cmd_say)
    ij = sub.add_parser("inject", help="test: create a thought from text (skips audio)")
    ij.add_argument("text")
    ij.set_defaults(fn=cmd_inject)
    sub.add_parser("run", help="run the daemon in the foreground without the menu bar").set_defaults(fn=cmd_run)
    sub.add_parser("install", help="build Ramble.app and start it at login").set_defaults(fn=cmd_install)
    sub.add_parser("uninstall", help="stop and remove the login item").set_defaults(fn=cmd_uninstall)
    a = p.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
