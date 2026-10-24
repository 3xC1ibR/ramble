"""Menu bar app: shows whether the mic is live and toggles it. Runs the daemon in-process."""

from __future__ import annotations

import subprocess
import time

import rumps

from . import config, state
from .daemon import Daemon, setup_logging

ON, OFF, PAUSED = "🎙", "🎙̸", "⏸"


class RambleApp(rumps.App):
    def __init__(self) -> None:
        super().__init__("Ramble", title=OFF, quit_button=None)
        self.daemon = Daemon()
        self.toggle_item = rumps.MenuItem("Turn on", callback=self.toggle)
        self.status_item = rumps.MenuItem("…")
        self.stats_item = rumps.MenuItem("…")
        self.menu = [
            self.toggle_item,
            rumps.MenuItem("Pause 1 hour", callback=lambda _: self.pause(3600)),
            rumps.MenuItem("Pause 3 hours", callback=lambda _: self.pause(3 * 3600)),
            None,
            self.status_item,
            self.stats_item,
            None,
            rumps.MenuItem("Open today's digest", callback=self.open_digest),
            rumps.MenuItem("Open data folder", callback=lambda _: subprocess.run(["open", str(config.HOME)])),
            rumps.MenuItem("Open log", callback=lambda _: subprocess.run(["open", "-a", "Console", str(config.LOG_DIR / "ramble.log")])),
            None,
            rumps.MenuItem("Quit Ramble", callback=self.quit),
        ]
        self.daemon.start()
        self.timer = rumps.Timer(self.tick, 1)
        self.timer.start()
        self.n = 0

    def toggle(self, _) -> None:
        state.write(not state.is_listening())
        self.tick(None)

    def pause(self, seconds: int) -> None:
        state.write(True, time.time() + seconds)
        self.tick(None)

    def open_digest(self, _) -> None:
        path = config.DIGEST_DIR / f"{time.strftime('%Y-%m-%d')}.md"
        if not path.exists():
            path.write_text(f"# Ramble digest {time.strftime('%Y-%m-%d')}\n\n")
        subprocess.run(["open", str(path)])

    def tick(self, _) -> None:
        s = state.read()
        listening = state.is_listening(s)
        rec = self.daemon.recorder
        if rec.error:
            self.title = "🎙⚠️"
        elif listening:
            self.title = ON
        elif s.get("enabled"):
            self.title = PAUSED
        else:
            self.title = OFF
        self.toggle_item.title = "Turn off" if listening else "Turn on"
        status = f"Mic {state.describe(s)}"
        if rec.error:
            status = f"Mic error: {rec.error[:60]}"
        elif self.daemon.router.current:
            status += " · triaging…"
        self.status_item.title = status
        self.n += 1
        if self.n % 5 == 1:
            st = self.daemon.stats()
            self.stats_item.title = (
                f"Today: {st['speech_min_today']} min speech · {st['thoughts_today']} thoughts · "
                f"{st['dispatched_today']} dispatched" + (f" · {st['pending']} queued" if st["pending"] else "")
            )

    def quit(self, _) -> None:
        self.daemon.shutdown()
        rumps.quit_application()


def main() -> None:
    setup_logging()
    RambleApp().run()


if __name__ == "__main__":
    main()
