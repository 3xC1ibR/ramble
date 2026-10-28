"""Menu bar app: shows whether the mic is live and toggles it. Runs the daemon in-process."""

from __future__ import annotations

import subprocess
import threading
import time

import rumps

from . import config, model_settings, state, voice_control
from .daemon import Daemon, setup_logging
from .mini_window import MiniWindow
from .app_window import RambleWindow

ON, OFF, PAUSED = "🎙", "🎙̸", "⏸"


class RambleApp(rumps.App):
    def __init__(self) -> None:
        super().__init__("Ramble", title=OFF, quit_button=None)
        self.daemon = Daemon()
        self.toggle_item = rumps.MenuItem("Turn on", callback=self.toggle)
        self.voice_item = rumps.MenuItem("Disable voice responses", callback=self.toggle_voice)
        self.mini_item = rumps.MenuItem("Show mini window", callback=self.toggle_mini)
        self.status_item = rumps.MenuItem("…")
        self.stats_item = rumps.MenuItem("…")
        self.models_item = rumps.MenuItem("Models and reasoning")
        self.models_item.add(rumps.MenuItem("Loading models…"))
        self.model_result = None
        self.model_snapshot = None
        self.model_busy = False
        self.model_refresh_at = 0
        self.model_error = None
        self.main_window = None
        self.menu = [
            rumps.MenuItem("Open Ramble…", callback=lambda _: self.open_window()),
            rumps.MenuItem("Token usage…", callback=lambda _: self.open_window("usage")),
            None,
            self.toggle_item,
            self.voice_item,
            self.mini_item,
            rumps.MenuItem("Reset mini window position", callback=lambda _: self.mini.reset_position()),
            rumps.MenuItem("Pause 1 hour", callback=lambda _: self.pause(3600)),
            rumps.MenuItem("Pause 3 hours", callback=lambda _: self.pause(3 * 3600)),
            None,
            self.status_item,
            self.stats_item,
            None,
            self.models_item,
            rumps.MenuItem("Global instructions…", callback=lambda _: self.open_settings()),
            rumps.MenuItem("Open today's digest", callback=self.open_digest),
            rumps.MenuItem("Open data folder", callback=lambda _: subprocess.run(["open", str(config.HOME)])),
            rumps.MenuItem("Open log", callback=lambda _: subprocess.run(["open", "-a", "Console", str(config.LOG_DIR / "ramble.log")])),
            None,
            rumps.MenuItem("Quit Ramble", callback=self.quit),
        ]
        self.daemon.start()
        self.mini = MiniWindow(self.daemon, model_menu=self.mini_model_menu, open_settings=self.open_window)
        self.mini_item.state = int(self.mini.visible)
        self.timer = rumps.Timer(self.tick, 1)
        self.timer.start()
        self.n = 0

    def refresh_models(self, settings=None, version=None):
        if self.model_busy:
            return
        self.model_busy = True
        def run():
            try:
                saved = (model_settings.snapshot() if settings is None else
                         model_settings.save(settings, version=version))
                self.model_result = (saved, None)
            except (OSError, ValueError) as exc:
                self.model_result = (None, str(exc))
            finally:
                self.model_busy = False
        threading.Thread(target=run, name="model-settings", daemon=True).start()
        self.model_refresh_at = time.monotonic() + 30

    def show_models(self, saved):
        if saved == self.model_snapshot:
            return
        self.model_snapshot = saved
        self.models_item.clear()
        for lane, (label, _, _) in model_settings.LANES.items():
            pair = saved['settings'][lane]
            section = rumps.MenuItem(f"{label}: {pair['model'] or 'Provider default'} · {pair['effort'] or 'default'}")
            self.models_item.add(section)
            if lane == 'triage' and saved['triage_backend'] != 'codex':
                section.add(rumps.MenuItem("Claude settings: edit config.json"))
                continue
            choices = ([{'id': None, 'label': 'Provider default', 'efforts': []}] if lane == 'work' else []) + saved['models']
            for model in choices:
                item = rumps.MenuItem(model['label'])
                item.state = int(pair['model'] == model['id'])
                section.add(item)
                efforts = ([None] if lane == 'work' else []) + model['efforts']
                for effort in efforts:
                    def choose(_, lane=lane, mid=model['id'], effort=effort):
                        settings = {key: dict(value) for key, value in saved['settings'].items()}
                        settings[lane] = {'model': mid, 'effort': effort}
                        self.refresh_models(settings, saved['version'])
                    option = rumps.MenuItem(effort.capitalize() if effort else 'Default reasoning', callback=choose)
                    option.state = int(pair == {'model': model['id'], 'effort': effort})
                    item.add(option)
        self.models_item.add(rumps.MenuItem("Open model settings…", callback=lambda _: self.open_settings()))
        self.models_item.add(rumps.MenuItem("Refresh models", callback=lambda _: self.refresh_models()))

    def mini_model_menu(self):
        if time.monotonic() >= self.model_refresh_at:
            self.refresh_models()
        # Reuse the native menu and its callbacks, including current selections,
        # supported reasoning levels, and loading/error/retry states.
        return self.models_item._menu

    def open_settings(self):
        self.open_window("settings")

    def open_window(self, page=None):
        if self.main_window is None:
            self.main_window = RambleWindow(self)
        self.main_window.show(page)

    def toggle(self, _) -> None:
        state.write(not state.is_listening())
        self.tick(None)

    def pause(self, seconds: int) -> None:
        state.write(True, time.time() + seconds)
        self.tick(None)

    def toggle_voice(self, _) -> None:
        voice_control.write(not voice_control.read()["enabled"])
        self.tick(None)

    def toggle_mini(self, _) -> None:
        self.mini.set_visible(not self.mini.visible)
        self.mini_item.state = int(self.mini.visible)

    def open_digest(self, _) -> None:
        path = config.DIGEST_DIR / f"{time.strftime('%Y-%m-%d')}.md"
        if not path.exists():
            path.write_text(f"# Ramble digest {time.strftime('%Y-%m-%d')}\n\n")
        subprocess.run(["open", str(path)])

    def tick(self, _) -> None:
        if self.model_result is not None and not self.mini.menu_open:
            saved, error = self.model_result
            self.model_result = None
            self.model_error = error
            if saved:
                self.show_models(saved)
            elif error:
                self.model_snapshot = None
                self.models_item.clear()
                self.models_item.add(rumps.MenuItem(error[:120]))
                self.models_item.add(rumps.MenuItem("Retry loading models", callback=lambda _: self.refresh_models()))
        if time.monotonic() >= self.model_refresh_at:
            self.refresh_models()
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
        voice_enabled = voice_control.read()["enabled"]
        self.voice_item.title = "Disable voice responses" if voice_enabled else "Enable voice responses"
        status = f"Mic {state.describe(s)}"
        if not voice_enabled:
            status += " · voice responses off"
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
        if self.main_window:
            self.main_window.close()
        self.mini.close()
        self.daemon.shutdown()
        rumps.quit_application()


def main() -> None:
    setup_logging()
    RambleApp().run()


if __name__ == "__main__":
    main()
