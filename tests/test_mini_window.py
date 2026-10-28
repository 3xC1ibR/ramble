"""Native window geometry/rendering without starting Ramble or audio devices."""
import json
import tempfile
import time
import unittest
from collections import deque
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import AppKit as A
import Foundation as F

from ramble import config
from ramble.activity import Activity
from ramble.mini_window import MiniWindow, COMPACT, EXPANDED, HOVER_COMPACT, HOVER_EXPANDED, fit_frame


class MiniWindowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        A.NSApplication.sharedApplication()

    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="ramble-test-native-", dir="/tmp")
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name)
        paths = patch.object(config, "HOME", self.home)
        paths.start()
        self.addCleanup(paths.stop)
        (self.home / "mini-window.json").write_text(json.dumps({"visible": False}))
        self.daemon = SimpleNamespace(recorder=SimpleNamespace(level=.1))
        self.monitor = SimpleNamespace(current=Activity(), output_level=.1, start=lambda: None)
        self.window = MiniWindow(self.daemon, monitor=self.monitor)
        self.addCleanup(self.window.close)

    def test_window_cannot_take_focus_and_joins_spaces(self):
        panel = self.window.panel
        self.assertFalse(panel.canBecomeKeyWindow())
        self.assertFalse(panel.canBecomeMainWindow())
        self.assertFalse(panel.hidesOnDeactivate())
        self.assertGreater(panel.level(), A.NSNormalWindowLevel)
        self.assertTrue(panel.styleMask() & A.NSWindowStyleMaskNonactivatingPanel)
        self.assertTrue(panel.collectionBehavior() & A.NSWindowCollectionBehaviorCanJoinAllSpaces)
        self.assertTrue(panel.collectionBehavior() & A.NSWindowCollectionBehaviorFullScreenAuxiliary)

    def test_position_and_visibility_survive_recreation(self):
        self.window.panel.setFrameOrigin_(F.NSMakePoint(220, 220))
        self.window.save_position()
        expected = dict(self.window.settings)
        second = MiniWindow(self.daemon, monitor=self.monitor)
        try:
            self.assertEqual(second.settings, expected)
            self.assertFalse(second.visible)
            self.assertAlmostEqual(second.panel.frame().origin.x, self.window.panel.frame().origin.x)
        finally:
            second.close()
        self.window.reset_position()
        self.assertEqual(self.window.settings, {"visible": False})

    def test_expansion_stays_on_display_even_for_disconnected_monitor_position(self):
        for center, bottom in ((-4000, -1000), (3000, 4000), (100, 80)):
            x, y, w, h = fit_frame((0, 30, 1024, 738), center, bottom, *EXPANDED)
            self.assertGreaterEqual(x, 8)
            self.assertGreaterEqual(y, 38)
            self.assertLessEqual(x+w, 1016)
            self.assertLessEqual(y+h, 760)

    def settle(self, start=100):
        self.window.last_tick = start
        for i in range(1, 25):
            with patch("ramble.mini_window.time.monotonic", return_value=start+i/30):
                self.window.tick()

    def test_hover_expands_and_leave_returns_to_current_activity(self):
        self.window.visible = True  # Exercise geometry without ordering a test window onscreen.
        self.monitor.current = Activity("idle", "Ready")
        with patch("ramble.mini_window.time.monotonic", return_value=100):
            self.window.view.mouseEntered_(None)
        self.settle()
        self.assertAlmostEqual(self.window.panel.frame().size.width, HOVER_COMPACT[0], delta=.3)
        self.assertFalse(self.window.view.model_button.isHidden())
        self.assertFalse(self.window.view.settings_button.isHidden())
        self.monitor.current = Activity("speaking", "Speaking")
        self.settle(101)
        self.assertAlmostEqual(self.window.panel.frame().size.width, HOVER_EXPANDED[0], delta=.3)
        self.assertEqual(self.window.view.activity.mode, "speaking")
        with patch("ramble.mini_window.time.monotonic", return_value=102):
            self.window.view.mouseExited_(None)
            self.assertTrue(self.window.view.controls_visible)
        self.settle(103)
        self.assertAlmostEqual(self.window.panel.frame().size.width, EXPANDED[0], delta=.3)
        self.assertTrue(self.window.view.model_button.isHidden())
        self.monitor.current = Activity("idle", "Ready")
        self.settle(104)
        self.assertAlmostEqual(self.window.panel.frame().size.width, COMPACT[0], delta=.3)

    def test_model_menu_holds_expansion_when_pointer_leaves(self):
        self.window.visible = True
        menu = Mock()
        self.window.model_menu = lambda: menu
        def track(*args):
            self.assertTrue(self.window.menu_open)
            self.window.view.mouseExited_(None)
            self.settle(time.monotonic()+1)
            self.assertTrue(self.window.view.controls_visible)
            self.assertFalse(self.window.view.model_button.isHidden())
        menu.popUpMenuPositioningItem_atLocation_inView_.side_effect = track
        self.window.view.model_button.performClick_(None)
        menu.popUpMenuPositioningItem_atLocation_inView_.assert_called_once()
        self.assertFalse(self.window.menu_open)

    def test_settings_button_invokes_action_without_dragging_or_changing_capture(self):
        self.window.open_settings = Mock()
        self.window.view.settings_button.performClick_(None)
        self.window.open_settings.assert_called_once_with()
        self.assertFalse(self.window.dragging)
        self.assertFalse((self.home / "state.json").exists())
        self.assertEqual(self.window.view.settings_button.accessibilityLabel(), "Open Ramble")
        self.window.view.updateTrackingAreas()
        self.assertTrue(self.window.view.tracking.options() & A.NSTrackingActiveAlways)

    def test_popup_model_selection_uses_existing_save_path(self):
        import rumps
        from ramble import model_settings
        from ramble.app import RambleApp
        from tests.test_model_settings import MODELS

        (self.home / "config.json").write_text(json.dumps({"dispatch_daily_cap": 0,
                                                           "fast_speak": False, "speak_questions": False}))
        app = RambleApp.__new__(RambleApp)
        app.models_item = rumps.MenuItem("Models and reasoning")
        app.models_item.add(rumps.MenuItem("Loading models…"))
        app.model_snapshot = app.model_result = None
        app.model_busy = False
        app.model_refresh_at = time.monotonic() + 60
        with patch.object(model_settings, "catalog", return_value=MODELS):
            app.show_models(model_settings.snapshot())
            self.window.model_menu = app.mini_model_menu
            menu = self.window.model_menu()
            self.assertIs(menu, app.models_item._menu)
            # Native menu callbacks must still find their original rumps owner.
            quick = menu.itemAtIndex_(0).submenu()
            fast = quick.itemAtIndex_(0).submenu()
            fast.performActionForItemAtIndex_(1)  # Fast / High
            deadline = time.monotonic() + 3
            while app.model_busy and time.monotonic() < deadline:
                time.sleep(.01)
            self.assertFalse(app.model_busy)
            saved, error = app.model_result
            self.assertIsNone(error)
            self.assertEqual(saved["settings"]["fast"], {"model": "test-fast", "effort": "high"})
            self.assertEqual(model_settings.snapshot()["settings"]["fast"], saved["settings"]["fast"])
        self.assertFalse((self.home / "state.json").exists())

    def test_each_state_renders_to_a_native_bitmap(self):
        labels = {"idle": "Ready", "off": "Mic off", "paused": "Paused", "awake": "I'm here",
                  "listening": "Listening", "speaking": "Speaking", "thinking": "Thinking",
                  "working": "Working", "processing": "Processing", "error": "Mic error"}
        for mode, label in labels.items():
            with self.subTest(mode=mode):
                view = self.window.view
                view.activity = Activity(mode, label)
                self.window.layout(*(COMPACT if view.activity.compact else EXPANDED))
                view.phase = 1
                view.levels = deque([.1, .2, .4, .6, .3, .1, .3, .8, .6, .4, .3, .2, .1], maxlen=13)
                bitmap = view.bitmapImageRepForCachingDisplayInRect_(view.bounds())
                view.cacheDisplayInRect_toBitmapImageRep_(view.bounds(), bitmap)
                png = bitmap.representationUsingType_properties_(A.NSBitmapImageFileTypePNG, {})
                self.assertGreater(len(png), 100)

    def test_hover_controls_render_alongside_idle_and_waveform(self):
        for activity in (Activity("idle", "Ready"), Activity("speaking", "Speaking")):
            self.window.view.activity = activity
            self.window.view.controls_visible = True
            self.window.layout(*(HOVER_COMPACT if activity.compact else HOVER_EXPANDED))
            view = self.window.view
            self.assertFalse(view.model_button.isHidden())
            bitmap = view.bitmapImageRepForCachingDisplayInRect_(view.bounds())
            view.cacheDisplayInRect_toBitmapImageRep_(view.bounds(), bitmap)
            self.assertGreater(len(bitmap.representationUsingType_properties_(A.NSBitmapImageFileTypePNG, {})), 100)


if __name__ == "__main__":
    unittest.main()
