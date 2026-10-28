"""A small, nonactivating AppKit panel sharing the menu app's main run loop."""
from __future__ import annotations

import json
import logging
import math
import time
from collections import deque

import AppKit as A
import Foundation as F
import objc

from . import config
from .activity import Activity, ActivityMonitor, display_level

log = logging.getLogger("ramble.mini")
COMPACT = (48.0, 22.0)
EXPANDED = (174.0, 38.0)
HOVER_COMPACT = (144.0, 34.0)
HOVER_EXPANDED = (246.0, 38.0)
COLORS = {
    "idle": (.52, .86, .73), "off": (.55, .57, .61), "paused": (.92, .70, .36),
    "listening": (.55, .91, .78), "awake": (.75, .69, 1.0),
    "speaking": (.75, .69, 1.0), "thinking": (.91, .76, .49),
    "working": (.67, .75, 1.0), "processing": (.63, .78, .94),
    "starting": (.63, .78, .94), "error": (1.0, .57, .50),
}


def color(rgb, alpha=1):
    return A.NSColor.colorWithCalibratedRed_green_blue_alpha_(*rgb, alpha)


def pill(rect, radius, tint):
    tint.setFill()
    A.NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(rect, radius, radius).fill()


def fit_frame(visible, center, bottom, width, height):
    """Keep the entire pill reachable after expansion or display changes."""
    x, y, w, h = visible
    left = min(max(center - width / 2, x + 8), x + w - width - 8)
    bottom = min(max(bottom, y + 8), y + h - height - 8)
    return left, bottom, width, height


class MiniPanel(A.NSPanel):
    def canBecomeKeyWindow(self):
        return False

    def canBecomeMainWindow(self):
        return False


class MiniButton(A.NSButton):
    def acceptsFirstMouse_(self, event):
        return True


class MiniView(A.NSView):
    def initWithFrame_(self, frame):
        self = objc.super(MiniView, self).initWithFrame_(frame)
        if self is None:
            return None
        self.owner = None
        self.activity = Activity()
        self.levels = deque([0.0] * 13, maxlen=13)
        self.phase = 0.0
        self.reduced_motion = False
        self.controls_visible = False
        self.tracking = None
        self.model_button = self.make_button("sparkles", "Choose model and reasoning", "chooseModel:")
        self.settings_button = self.make_button("arrow.up.left.and.arrow.down.right",
                                                "Open Ramble", "openSettings:")
        self.setAccessibilityElement_(True)
        self.setAccessibilityRole_(A.NSAccessibilityGroupRole)
        self.setAccessibilityLabel_("Ramble · microphone off")
        return self

    @objc.python_method
    def make_button(self, symbol, label, action):
        button = MiniButton.alloc().initWithFrame_(F.NSMakeRect(0, 0, 30, 28))
        button.setBordered_(False)
        button.setButtonType_(A.NSButtonTypeMomentaryChange)
        button.setImage_(A.NSImage.imageWithSystemSymbolName_accessibilityDescription_(symbol, label))
        button.setImagePosition_(A.NSImageOnly)
        button.setContentTintColor_(color((.94, .95, .98)))
        button.setTarget_(self)
        button.setAction_(action)
        button.setRefusesFirstResponder_(True)
        button.setAccessibilityLabel_(label)
        button.setToolTip_(label)
        button.setHidden_(True)
        self.addSubview_(button)
        return button

    @objc.python_method
    def layout_controls(self):
        size = self.bounds().size
        normal, expanded = (COMPACT, HOVER_COMPACT) if self.activity.compact else (EXPANDED, HOVER_EXPANDED)
        reveal = self.controls_visible and size.width >= normal[0] + .75 * (expanded[0] - normal[0])
        for button, x in ((self.model_button, 6), (self.settings_button, size.width - 36)):
            button.setFrame_(F.NSMakeRect(x, (size.height - 28) / 2, 30, 28))
            button.setHidden_(not reveal)

    def updateTrackingAreas(self):
        objc.super(MiniView, self).updateTrackingAreas()
        if self.tracking:
            self.removeTrackingArea_(self.tracking)
        self.tracking = A.NSTrackingArea.alloc().initWithRect_options_owner_userInfo_(
            F.NSZeroRect, A.NSTrackingMouseEnteredAndExited | A.NSTrackingActiveAlways
            | A.NSTrackingInVisibleRect | A.NSTrackingEnabledDuringMouseDrag, self, None)
        self.addTrackingArea_(self.tracking)

    def mouseEntered_(self, event):
        if self.owner:
            self.owner.set_hovered(True)

    def mouseExited_(self, event):
        if self.owner:
            self.owner.set_hovered(False)

    def chooseModel_(self, sender):
        if self.owner:
            self.owner.show_models()

    def openSettings_(self, sender):
        if self.owner and self.owner.open_settings:
            self.owner.open_settings()

    def isOpaque(self):
        return False

    def acceptsFirstMouse_(self, event):
        return True

    def mouseDown_(self, event):
        if self.owner:
            self.owner.dragging = True
            try:
                self.window().performWindowDragWithEvent_(event)
            finally:
                self.owner.dragging = False
                self.owner.save_position()

    def rightMouseDown_(self, event):
        if self.owner:
            self.owner.popup(self.owner.menu)

    def advance_(self, timer):
        if self.owner:
            self.owner.tick()

    def resetPosition_(self, sender):
        self.owner.reset_position()

    def drawRect_(self, dirty):
        bounds = self.bounds()
        width, height = bounds.size.width, bounds.size.height
        rect = F.NSMakeRect(1, 1, width - 2, height - 2)
        radius = (height - 2) / 2
        background = A.NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(rect, radius, radius)
        color((.075, .082, .10), .97).setFill()
        background.fill()
        color((1, 1, 1), .16).setStroke()
        background.setLineWidth_(.7)
        background.stroke()
        mode = self.activity.mode
        rgb = COLORS["awake" if self.activity.awake else mode]
        mid = height / 2
        if self.activity.compact:
            offset = (width - COMPACT[0]) / 2
            if self.controls_visible:
                pill(F.NSMakeRect(width / 2 - 19, mid - 13, 38, 26), 13, color((.20, .21, .24)))
            if mode == "paused":
                for x in (14, 19):
                    pill(F.NSMakeRect(offset + x, mid - 3.5, 2, 7), 1, color(rgb))
            elif mode == "off":
                ring = A.NSBezierPath.bezierPathWithOvalInRect_(F.NSMakeRect(offset + 13, mid - 3, 6, 6))
                color(rgb).setStroke()
                ring.setLineWidth_(1)
                ring.stroke()
            else:
                pill(F.NSMakeRect(offset + 13, mid - 3, 6, 6), 3, color(rgb))
            pill(F.NSMakeRect(offset + 25, mid - 1.5, 10, 3), 1.5, color((.60, .63, .69), .7))
            return
        # Clip during the size transition; draw at final size around its center.
        A.NSGraphicsContext.saveGraphicsState()
        background.addClip()
        offset = (width - EXPANDED[0]) / 2
        x = offset + 18
        if mode == "error":
            pill(F.NSMakeRect(x - 1, mid - 1, 2, 7), 1, color(rgb))
            pill(F.NSMakeRect(x - 1, mid - 5, 2, 2), 1, color(rgb))
        elif mode in {"awake", "working"}:
            pulse = 0 if self.reduced_motion else (math.sin(self.phase * 3) + 1) / 2
            ring = A.NSBezierPath.bezierPathWithOvalInRect_(F.NSMakeRect(x - 5, mid - 5, 10, 10))
            color(rgb, .45 + .4 * pulse).setStroke()
            ring.setLineWidth_(1.3)
            ring.stroke()
            pill(F.NSMakeRect(x - 1.8, mid - 1.8, 3.6, 3.6), 1.8, color(rgb))
        else:
            pill(F.NSMakeRect(x - 3, mid - 3, 6, 6), 3, color(rgb))
        attrs = {
            A.NSFontAttributeName: A.NSFont.systemFontOfSize_weight_(11, A.NSFontWeightMedium),
            A.NSForegroundColorAttributeName: color((.94, .95, .98)),
        }
        F.NSString.stringWithString_(self.activity.label).drawAtPoint_withAttributes_(
            F.NSMakePoint(offset + 31, mid - 7), attrs)
        if mode in {"listening", "speaking"}:
            for i, level in enumerate(self.levels):
                bar_height = 3 + level * 18
                pill(F.NSMakeRect(offset + 112 + i * 3.6, mid - bar_height / 2, 2.1, bar_height),
                     1.05, color(rgb, .6 + .4 * (i / 12)))
        elif mode == "awake":
            for i, h in enumerate((4, 8, 13, 8, 4)):
                scale = 1 if self.reduced_motion else .8 + .2 * math.sin(self.phase * 3)
                pill(F.NSMakeRect(offset + 124 + i * 5, mid - h * scale / 2, 2.5, h * scale),
                     1.25, color(rgb))
        elif mode != "error":
            # An indeterminate animation, deliberately distinct from audio bars.
            for i in range(3):
                pulse = .5 if self.reduced_motion else (math.sin(self.phase * 4 - i * .9) + 1) / 2
                dot_size = 3 + pulse
                pill(F.NSMakeRect(offset + 124 + i * 10, mid - dot_size / 2, dot_size, dot_size),
                     dot_size / 2, color(rgb, .35 + .65 * pulse))
        A.NSGraphicsContext.restoreGraphicsState()


class MiniWindow:
    def __init__(self, daemon, *, monitor=None, model_menu=None, open_settings=None):
        self.monitor = monitor or ActivityMonitor(daemon)
        self.daemon = daemon
        self.path = config.HOME / "mini-window.json"
        self.settings = self._load()
        self.visible = self.settings.get("visible", True) is not False
        self.dragging = False
        self.hovered = self.menu_open = False
        self.hover_until = 0.0
        self.model_menu = model_menu
        self.open_settings = open_settings
        self.last_tick = time.monotonic()
        self.last_layout = 0.0
        self.smoothed_level = 0.0
        self.timer = None
        self.interval = None
        self.panel = MiniPanel.alloc().initWithContentRect_styleMask_backing_defer_(
            F.NSMakeRect(0, 0, *COMPACT),
            A.NSWindowStyleMaskBorderless | A.NSWindowStyleMaskNonactivatingPanel,
            A.NSBackingStoreBuffered, False)
        self.panel.setTitle_("Ramble mini window")
        self.panel.setOpaque_(False)
        self.panel.setBackgroundColor_(A.NSColor.clearColor())
        self.panel.setHasShadow_(True)
        self.panel.setLevel_(A.NSFloatingWindowLevel)
        self.panel.setHidesOnDeactivate_(False)
        self.panel.setBecomesKeyOnlyIfNeeded_(True)
        self.panel.setCollectionBehavior_(A.NSWindowCollectionBehaviorCanJoinAllSpaces
                                         | A.NSWindowCollectionBehaviorFullScreenAuxiliary
                                         | A.NSWindowCollectionBehaviorStationary
                                         | A.NSWindowCollectionBehaviorIgnoresCycle
                                         | getattr(A, "NSWindowCollectionBehaviorCanJoinAllApplications", 0))
        self.view = MiniView.alloc().initWithFrame_(F.NSMakeRect(0, 0, *COMPACT))
        self.view.owner = self
        self.panel.setContentView_(self.view)
        self.menu = A.NSMenu.alloc().initWithTitle_("Ramble")
        reset = A.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_("Reset position", "resetPosition:", "")
        reset.setTarget_(self.view)
        for label, action in (("Models and reasoning…", "chooseModel:"), ("Open Ramble…", "openSettings:")):
            item = A.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(label, action, "")
            item.setTarget_(self.view)
            self.menu.addItem_(item)
        self.menu.addItem_(A.NSMenuItem.separatorItem())
        self.menu.addItem_(reset)
        self.layout(*COMPACT)
        self.monitor.start()
        self._set_timer(.2)
        if self.visible:
            self.panel.orderFrontRegardless()

    def set_hovered(self, hovered):
        self.hovered = bool(hovered)
        # A short grace period bridges the expanding edge and popup menu.
        self.hover_until = 0.0 if hovered else time.monotonic() + .2
        self.tick()

    def show_models(self):
        if self.model_menu:
            menu = self.model_menu()
            if menu is not None:
                self.popup(menu)

    def popup(self, menu):
        self.menu_open = True
        self.tick()
        try:
            menu.popUpMenuPositioningItem_atLocation_inView_(
                None, F.NSMakePoint(12, self.view.bounds().size.height + 4), self.view)
        finally:
            self.menu_open = False
            self.set_hovered(F.NSPointInRect(A.NSEvent.mouseLocation(), self.panel.frame()))

    def _load(self):
        try:
            saved = json.loads(self.path.read_text())
            return saved if isinstance(saved, dict) else {}
        except (OSError, ValueError):
            return {}

    def _save(self):
        try:
            temp = self.path.with_suffix(".tmp")
            temp.write_text(json.dumps(self.settings))
            temp.replace(self.path)
        except OSError:
            log.warning("Could not save mini window position", exc_info=True)

    def _set_timer(self, interval):
        if self.interval == interval:
            return
        if self.timer:
            self.timer.invalidate()
        self.interval = interval
        self.timer = F.NSTimer.timerWithTimeInterval_target_selector_userInfo_repeats_(
            interval, self.view, "advance:", None, True)
        F.NSRunLoop.mainRunLoop().addTimer_forMode_(self.timer, F.NSRunLoopCommonModes)

    def screen(self):
        screens = list(A.NSScreen.screens())
        selected = self.settings.get("screen")
        return next((s for s in screens if str(s.deviceDescription()["NSScreenNumber"]) == selected),
                    A.NSScreen.mainScreen() or screens[0])

    def layout(self, width, height):
        screen = self.screen()
        visible = screen.visibleFrame()
        x, y = visible.origin.x, visible.origin.y
        w, h = visible.size.width, visible.size.height
        px, py = self.settings.get("x", .5), self.settings.get("y", 18 / h)
        if not isinstance(px, (float, int)) or not math.isfinite(px):
            px = .5
        if not isinstance(py, (float, int)) or not math.isfinite(py):
            py = 18 / h
        left, bottom, width, height = fit_frame((x, y, w, h), x + px * w, y + py * h, width, height)
        self.panel.setFrame_display_(F.NSMakeRect(left, bottom, width, height), True)
        self.view.layout_controls()

    def save_position(self):
        frame = self.panel.frame()
        screen = self.panel.screen() or self.screen()
        visible = screen.visibleFrame()
        self.settings.update(screen=str(screen.deviceDescription()["NSScreenNumber"]),
                             x=(frame.origin.x + frame.size.width / 2 - visible.origin.x) / visible.size.width,
                             y=(frame.origin.y - visible.origin.y) / visible.size.height)
        self._save()
        self.layout(frame.size.width, frame.size.height)

    def reset_position(self):
        self.settings = {"visible": self.visible}
        self._save()
        frame = self.panel.frame()
        self.layout(frame.size.width, frame.size.height)

    def set_visible(self, visible):
        self.visible = bool(visible)
        self.settings["visible"] = self.visible
        self._save()
        if visible:
            self.view.setNeedsDisplay_(True)
            self.panel.orderFrontRegardless()
        else:
            self.hovered = False
            self.hover_until = 0.0
            self.panel.orderOut_(None)

    def tick(self):
        now = time.monotonic()
        dt = min(.2, max(0.0, now - self.last_tick))
        self.last_tick = now
        current = self.monitor.current
        changed = current != self.view.activity
        if changed:
            self.view.levels = deque([0.0] * 13, maxlen=13)
            self.smoothed_level = 0.0
            self.view.activity = current
            self.view.setAccessibilityLabel_(current.detail)
            self.view.setToolTip_(current.detail + " · Drag to move")
        if not self.visible:
            self._set_timer(.2)
            return
        self.view.reduced_motion = A.NSWorkspace.sharedWorkspace().accessibilityDisplayShouldReduceMotion()
        controls = self.hovered or self.menu_open or self.dragging or now < self.hover_until
        controls_changed = controls != self.view.controls_visible
        self.view.controls_visible = controls
        target = ((HOVER_COMPACT if current.compact else HOVER_EXPANDED) if controls else
                  COMPACT if current.compact else EXPANDED)
        frame = self.panel.frame()
        resizing = abs(frame.size.width - target[0]) > .2 or abs(frame.size.height - target[1]) > .2
        if not self.dragging and (resizing or now - self.last_layout > 1):
            # AppKit rounds frames to device pixels. Snap the last two points
            # so interpolation cannot stall and keep the animation timer busy.
            settled = max(abs(frame.size.width - target[0]), abs(frame.size.height - target[1])) <= 2
            blend = 1 if self.view.reduced_motion or settled else 1 - math.exp(-dt / .07)
            self.layout(frame.size.width + (target[0] - frame.size.width) * blend,
                        frame.size.height + (target[1] - frame.size.height) * blend)
            self.last_layout = now
        self.view.layout_controls()
        self._set_timer(1 / 30 if not current.compact or resizing else .2)
        level = (self.monitor.output_level if current.mode == "speaking" else
                 self.daemon.recorder.level if current.mode == "listening" else 0.0)
        target_level = display_level(level)
        self.smoothed_level += (target_level - self.smoothed_level) * (1 - math.exp(-dt / .045))
        self.view.levels.append(self.smoothed_level)
        self.view.phase = now
        if changed or controls_changed or resizing or not current.compact:
            self.view.setNeedsDisplay_(True)

    def close(self):
        if self.timer:
            self.timer.invalidate()
        self.panel.orderOut_(None)
        self.view.owner = None
