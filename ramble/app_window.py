"""Ramble's reusable native Settings and Token Usage window."""
from __future__ import annotations

import logging
import threading
import time

import AppKit as A
import Foundation as F
import objc

from . import instructions, model_settings, state, usage, voice_control

log = logging.getLogger("ramble.window")


def text(value, size=13, *, bold=False, secondary=False):
    view = A.NSTextField.labelWithString_(value)
    view.setFont_(A.NSFont.systemFontOfSize_weight_(size, A.NSFontWeightSemibold if bold else A.NSFontWeightRegular))
    view.setTextColor_(A.NSColor.secondaryLabelColor() if secondary else A.NSColor.labelColor())
    view.setLineBreakMode_(A.NSLineBreakByTruncatingTail)
    return view


def button(title, target, action, *, tag=0):
    view = A.NSButton.buttonWithTitle_target_action_(title, target, action)
    view.setTag_(tag)
    return view


def place(view, x, y, width, height):
    view.setFrame_(F.NSMakeRect(x, y, max(1, width), max(1, height)))


def number(counts, key, missing=None):
    if key not in counts:
        return "—"
    prefix = "≥ " if missing and missing.get(key) else ""
    return prefix + f"{counts[key]:,}"


class Page(A.NSView):
    def isFlipped(self):
        return True

    def drawRect_(self, rect):
        background = getattr(self, "background", None)
        if background:
            background.setFill()
            A.NSRectFill(self.bounds())

    def resizeSubviewsWithOldSize_(self, old):
        objc.super(Page, self).resizeSubviewsWithOldSize_(old)
        owner = getattr(self, "owner", None)
        if owner:
            owner.layout()


class TableData(F.NSObject):
    def numberOfRowsInTableView_(self, table):
        return len(self.rows)

    def tableView_objectValueForTableColumn_row_(self, table, column, row):
        return self.rows[row].get(str(column.identifier()), "")


class Actions(F.NSObject):
    def showSettings_(self, sender):
        self.owner.show("settings")

    def showUsage_(self, sender):
        self.owner.show("usage")

    def quitApp_(self, sender):
        self.owner.app.quit(None)

    def selectPage_(self, sender):
        self.owner.select_page("usage" if sender.tag() else "settings")

    def modelMenu_(self, sender):
        self.owner.choose_model(sender)

    def refreshModels_(self, sender):
        self.owner.app.refresh_models()

    def saveInstructions_(self, sender):
        self.owner.save_instructions()

    def reloadInstructions_(self, sender):
        self.owner.reload_instructions()

    def toggleMic_(self, sender):
        self.owner.app.toggle(None)

    def toggleVoice_(self, sender):
        self.owner.app.toggle_voice(None)

    def toggleMini_(self, sender):
        self.owner.app.toggle_mini(None)

    def closeWindow_(self, sender):
        self.owner.window.performClose_(None)

    def refreshUsage_(self, sender):
        self.owner.refresh_usage()

    def changePeriod_(self, sender):
        self.owner.days = (1, 7, None)[sender.selectedSegment()]
        self.owner.refresh_usage()

    def changeGrouping_(self, sender):
        self.owner.render_usage()

    def tick_(self, timer):
        self.owner.tick()


class RambleWindow:
    def __init__(self, app, *, store_factory=usage.UsageStore):
        self.app = app
        self.store_factory = store_factory
        self.page = "settings"
        self.days = 1
        self.report = None
        self.usage_result = None
        self.usage_progress = None
        self.usage_busy = False
        self.refresh_again = False
        self.usage_refresh_at = 0
        self.instructions_version = None
        self.instructions_text = ""
        self.actions = Actions.alloc().init()
        self.actions.owner = self
        self.install_main_menu()
        self.window = A.NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            F.NSMakeRect(0, 0, 1060, 780), A.NSWindowStyleMaskTitled | A.NSWindowStyleMaskClosable
            | A.NSWindowStyleMaskMiniaturizable | A.NSWindowStyleMaskResizable,
            A.NSBackingStoreBuffered, False)
        self.window.setTitle_("Ramble")
        self.window.setReleasedWhenClosed_(False)
        self.window.setContentMinSize_(F.NSMakeSize(920, 720))
        self.window.center()
        self.root = Page.alloc().initWithFrame_(F.NSMakeRect(0, 0, 1060, 780))
        self.root.background = A.NSColor.windowBackgroundColor()
        self.window.setContentView_(self.root)
        self.sidebar = Page.alloc().initWithFrame_(F.NSZeroRect)
        self.sidebar.background = A.NSColor.controlBackgroundColor()
        self.root.addSubview_(self.sidebar)
        self.brand = text("Ramble", 25, bold=True)
        self.tagline = text("Your voice workspace", 12, secondary=True)
        self.sidebar.addSubview_(self.brand)
        self.sidebar.addSubview_(self.tagline)
        self.nav = []
        for i, title in enumerate(("Settings", "Token usage")):
            control = button(title, self.actions, "selectPage:", tag=i)
            control.setBezelStyle_(A.NSBezelStyleRecessed)
            control.setButtonType_(A.NSButtonTypePushOnPushOff)
            self.sidebar.addSubview_(control)
            self.nav.append(control)
        self.back = button("Back to mini window", self.actions, "closeWindow:")
        self.sidebar.addSubview_(self.back)
        self.settings_page = Page.alloc().initWithFrame_(F.NSZeroRect)
        self.usage_page = Page.alloc().initWithFrame_(F.NSZeroRect)
        self.root.addSubview_(self.settings_page)
        self.root.addSubview_(self.usage_page)
        self.build_settings()
        self.build_usage()
        self.root.owner = self
        self.layout()
        self.select_page("settings")
        self.load_instructions()
        self.timer = F.NSTimer.timerWithTimeInterval_target_selector_userInfo_repeats_(
            .2, self.actions, "tick:", None, True)
        F.NSRunLoop.mainRunLoop().addTimer_forMode_(self.timer, F.NSRunLoopCommonModes)

    def add(self, page, view):
        page.addSubview_(view)
        return view

    def install_main_menu(self):
        """Supply standard responder-chain shortcuts for this menu-bar app."""
        app = A.NSApplication.sharedApplication()
        if app.mainMenu() and app.mainMenu().numberOfItems():
            return
        bar = A.NSMenu.alloc().initWithTitle_("Ramble")
        for title, entries in (
            ("Ramble", [("Settings…", "showSettings:", ",", self.actions),
                        ("Token usage", "showUsage:", "2", self.actions),
                        ("Quit Ramble", "quitApp:", "q", self.actions)]),
            ("Edit", [("Undo", "undo:", "z", None), ("Redo", "redo:", "Z", None),
                      ("Cut", "cut:", "x", None), ("Copy", "copy:", "c", None),
                      ("Paste", "paste:", "v", None), ("Select All", "selectAll:", "a", None)]),
            ("Window", [("Minimize", "performMiniaturize:", "m", None),
                        ("Close", "performClose:", "w", None)]),
        ):
            root = A.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(title, None, "")
            menu = A.NSMenu.alloc().initWithTitle_(title)
            for label, action, key, target in entries:
                item = A.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(label, action, key)
                item.setTarget_(target)
                menu.addItem_(item)
            root.setSubmenu_(menu)
            bar.addItem_(root)
        app.setMainMenu_(bar)

    def build_settings(self):
        page = self.settings_page
        self.settings_title = self.add(page, text("Settings", 27, bold=True))
        self.settings_description = self.add(page, text("Make Ramble work the way you do.", secondary=True))
        self.switches = []
        for label, action in (("Microphone", "toggleMic:"), ("Voice responses", "toggleVoice:"),
                              ("Show mini window", "toggleMini:")):
            control = button(label, self.actions, action)
            control.setButtonType_(A.NSButtonTypeSwitch)
            self.add(page, control)
            self.switches.append(control)
        self.capture_status = self.add(page, text("", 12, secondary=True))
        self.models_title = self.add(page, text("Models & reasoning", 17, bold=True))
        self.refresh_models_button = self.add(page, button("Refresh models", self.actions, "refreshModels:"))
        self.model_rows = []
        for i, (lane, (label, _, _)) in enumerate(model_settings.LANES.items()):
            caption = self.add(page, text(label))
            picker = self.add(page, button("Loading models…", self.actions, "modelMenu:", tag=i))
            self.model_rows.append((lane, caption, picker))
        self.model_status = self.add(page, text("", 12, secondary=True))
        self.instructions_title = self.add(page, text("Global instructions", 17, bold=True))
        self.instructions_description = self.add(page, text("Shared with Companion. Applied to future replies and thought review.", 12, secondary=True))
        self.editor_scroll = self.add(page, A.NSScrollView.alloc().initWithFrame_(F.NSZeroRect))
        self.editor_scroll.setHasVerticalScroller_(True)
        self.editor_scroll.setBorderType_(A.NSBezelBorder)
        self.editor = A.NSTextView.alloc().initWithFrame_(F.NSMakeRect(0, 0, 600, 200))
        self.editor.setRichText_(False)
        self.editor.setAllowsUndo_(True)
        self.editor.setFont_(A.NSFont.systemFontOfSize_(13))
        self.editor.setTextContainerInset_(F.NSMakeSize(10, 10))
        self.editor.setVerticallyResizable_(True)
        self.editor.setHorizontallyResizable_(False)
        self.editor.setAutoresizingMask_(A.NSViewWidthSizable)
        self.editor.textContainer().setWidthTracksTextView_(True)
        self.editor_scroll.setDocumentView_(self.editor)
        self.save_button = self.add(page, button("Save instructions", self.actions, "saveInstructions:"))
        self.reload_button = self.add(page, button("Reload saved", self.actions, "reloadInstructions:"))
        self.instructions_status = self.add(page, text("", 12, secondary=True))

    def table(self, columns):
        scroll = A.NSScrollView.alloc().initWithFrame_(F.NSZeroRect)
        scroll.setHasVerticalScroller_(True)
        scroll.setHasHorizontalScroller_(True)
        scroll.setBorderType_(A.NSBezelBorder)
        table = A.NSTableView.alloc().initWithFrame_(F.NSZeroRect)
        table.setColumnAutoresizingStyle_(A.NSTableViewNoColumnAutoresizing)
        table.setIntercellSpacing_(F.NSMakeSize(4, 2))
        table.setUsesAlternatingRowBackgroundColors_(True)
        table.setRowHeight_(30)
        for key, title, width in columns:
            col = A.NSTableColumn.alloc().initWithIdentifier_(key)
            col.headerCell().setStringValue_(title)
            col.setWidth_(width)
            col.setMinWidth_(40)
            col.setEditable_(False)
            col.dataCell().setFont_(A.NSFont.systemFontOfSize_(11))
            col.dataCell().setLineBreakMode_(A.NSLineBreakByTruncatingMiddle)
            table.addTableColumn_(col)
        source = TableData.alloc().init()
        source.rows = []
        table.setDataSource_(source)
        scroll.setDocumentView_(table)
        return scroll, table, source

    def build_usage(self):
        page = self.usage_page
        self.usage_title = self.add(page, text("Token usage", 27, bold=True))
        self.usage_description = self.add(page, text("See where Ramble's model calls go.", secondary=True))
        self.period = self.add(page, A.NSSegmentedControl.segmentedControlWithLabels_trackingMode_target_action_(
            ["Today", "7 days", "All recorded"], A.NSSegmentSwitchTrackingSelectOne, self.actions, "changePeriod:"))
        self.period.setSelectedSegment_(0)
        self.refresh_usage_button = self.add(page, button("Refresh", self.actions, "refreshUsage:"))
        self.cards = []
        for label in ("Recorded tokens", "Input", "Cached input", "Output"):
            box = self.add(page, A.NSBox.alloc().initWithFrame_(F.NSZeroRect))
            box.setTitlePosition_(A.NSNoTitle)
            box.setBoxType_(A.NSBoxCustom)
            box.setBorderType_(A.NSLineBorder)
            box.setCornerRadius_(10)
            box.setBorderColor_(A.NSColor.separatorColor())
            box.setFillColor_(A.NSColor.controlBackgroundColor())
            value, caption = text("—", 23, bold=True), text(label, 12, secondary=True)
            box.contentView().addSubview_(value)
            box.contentView().addSubview_(caption)
            self.cards.append((box, value, caption))
        self.usage_status = self.add(page, text("Loading recorded usage…", 12, secondary=True))
        self.attribution_title = self.add(page, text("Attribution", 17, bold=True))
        self.grouping = self.add(page, A.NSSegmentedControl.segmentedControlWithLabels_trackingMode_target_action_(
            ["Activity & model", "Recent calls"], A.NSSegmentSwitchTrackingSelectOne, self.actions, "changeGrouping:"))
        self.grouping.setSelectedSegment_(0)
        self.attribution_scroll, self.attribution_table, self.attribution_data = self.table([
            ("activity", "Activity / request", 140), ("model", "Provider · model", 150),
            ("calls", "Calls / time", 65), ("input", "Input", 75), ("cache", "Cached", 70),
            ("output", "Output", 65), ("total", "Tokens", 90), ("coverage", "Coverage / outcome", 130)])
        self.add(page, self.attribution_scroll)
        self.linked_title = self.add(page, text("Linked work sessions", 17, bold=True))
        self.linked_description = self.add(page, text("Lifetime conversation totals. Kept separate from the dated calls above.", 12, secondary=True))
        self.linked_scroll, self.linked_table, self.linked_data = self.table([
            ("name", "Session", 245), ("provider", "Provider / model", 150), ("tokens", "Tokens", 100),
            ("scope", "Includes", 135), ("status", "Coverage · observed", 170)])
        self.add(page, self.linked_scroll)
        self.usage_note = self.add(page, text("Cache is included in input. Missing counts stay unknown; ≥ marks a measured subtotal.", 12, secondary=True))
        self.usage_note2 = self.add(page, text("Retained history can be incomplete. Local Whisper and speech-audio billing are outside these token counts.", 12, secondary=True))

    def layout(self):
        w, h = self.root.bounds().size.width, self.root.bounds().size.height
        place(self.sidebar, 0, 0, 184, h)
        place(self.brand, 18, 30, 150, 32)
        place(self.tagline, 18, 68, 154, 20)
        for i, control in enumerate(self.nav):
            place(control, 12, 122 + i * 42, 160, 34)
        place(self.back, 12, h - 52, 160, 30)
        for page in (self.settings_page, self.usage_page):
            place(page, 184, 0, w - 184, h)
        width = w - 184 - 48
        for title in (self.settings_title, self.usage_title):
            place(title, 24, 28, width, 36)
        for desc in (self.settings_description, self.usage_description):
            place(desc, 24, 72, width, 22)
        for i, control in enumerate(self.switches):
            place(control, 24 + i * width / 3, 116, width / 3, 26)
        place(self.capture_status, 24, 150, width, 22)
        place(self.models_title, 24, 196, width - 150, 26)
        place(self.refresh_models_button, width - 120, 192, 144, 30)
        for i, (_, label, control) in enumerate(self.model_rows):
            place(label, 24, 240 + i * 42, 180, 26)
            place(control, 214, 237 + i * 42, width - 190, 32)
        place(self.model_status, 24, 369, width, 24)
        place(self.instructions_title, 24, 411, width, 26)
        place(self.instructions_description, 24, 444, width, 22)
        place(self.editor_scroll, 24, 478, width, h - 580)
        place(self.save_button, 24, h - 88, 150, 32)
        place(self.reload_button, 184, h - 88, 130, 32)
        place(self.instructions_status, 24, h - 45, width, 24)
        place(self.period, 24, 112, 290, 30)
        place(self.refresh_usage_button, width - 72, 112, 96, 30)
        card_width = (width - 36) / 4
        for i, (box, value, caption) in enumerate(self.cards):
            place(box, 24 + i * (card_width + 12), 160, card_width, 86)
            place(value, 12, 34, card_width - 24, 32)
            place(caption, 12, 10, card_width - 24, 20)
        place(self.usage_status, 24, 259, width, 22)
        place(self.attribution_title, 24, 303, width - 300, 26)
        place(self.grouping, width - 246, 300, 270, 30)
        table_height = max(120, h - 600)
        place(self.attribution_scroll, 24, 342, width, table_height)
        linked_y = 360 + table_height
        place(self.linked_title, 24, linked_y, width, 26)
        place(self.linked_description, 24, linked_y + 32, width, 22)
        place(self.linked_scroll, 24, linked_y + 62, width, 104)
        place(self.usage_note, 24, h - 58, width, 20)
        place(self.usage_note2, 24, h - 33, width, 20)

    def show(self, page=None):
        if page:
            self.select_page(page)
        self.window.makeKeyAndOrderFront_(None)
        A.NSApplication.sharedApplication().activateIgnoringOtherApps_(True)
        self.tick()

    def select_page(self, page):
        self.page = page
        self.settings_page.setHidden_(page != "settings")
        self.usage_page.setHidden_(page != "usage")
        for control, name in zip(self.nav, ("settings", "usage")):
            control.setState_(int(page == name))
        if page == "usage":
            self.refresh_usage()

    def choose_model(self, sender):
        menu = self.app.mini_model_menu()
        if self.app.model_snapshot and menu.numberOfItems() > sender.tag():
            menu = menu.itemAtIndex_(sender.tag()).submenu() or menu
        self.app.mini.menu_open = True
        try:
            menu.popUpMenuPositioningItem_atLocation_inView_(None, F.NSMakePoint(0, sender.bounds().size.height), sender)
        finally:
            self.app.mini.menu_open = False

    def load_instructions(self):
        try:
            saved = instructions.snapshot()
            self.editor.setString_(saved["text"])
            self.instructions_version = saved["version"]
            self.instructions_text = saved["text"]
            self.instructions_status.setStringValue_("Saved instructions loaded.")
        except (OSError, ValueError) as exc:
            self.instructions_status.setStringValue_(str(exc))

    def reload_instructions(self):
        if str(self.editor.string()) != self.instructions_text:
            alert = A.NSAlert.alloc().init()
            alert.setMessageText_("Reload saved instructions?")
            alert.setInformativeText_("This replaces your unsaved draft with the saved instructions.")
            alert.addButtonWithTitle_("Keep editing")
            alert.addButtonWithTitle_("Reload saved")
            if alert.runModal() != A.NSAlertSecondButtonReturn:
                return
        self.load_instructions()

    def save_instructions(self):
        if self.instructions_version is None:
            self.instructions_status.setStringValue_("Reload saved instructions before saving.")
            return
        try:
            saved = instructions.save(str(self.editor.string()), version=self.instructions_version)
            self.instructions_version = saved["version"]
            self.instructions_text = saved["text"]
            self.instructions_status.setStringValue_("Instructions saved. Future requests will use them.")
        except (OSError, ValueError) as exc:
            self.instructions_status.setStringValue_(str(exc))

    def refresh_usage(self):
        if self.usage_busy:
            self.refresh_again = True
            return
        self.usage_busy = True
        self.refresh_again = False
        days = self.days
        self.usage_status.setStringValue_("Refreshing recorded usage…")
        self.refresh_usage_button.setEnabled_(False)
        def read():
            try:
                store = self.store_factory()
                skipped = store.recover()
                local = store.report(days)
                local["skipped"] = skipped
                self.usage_progress = (days, local)
                store.refresh_linked()
                report = store.report(days)
                report["skipped"] = skipped
                self.usage_result = (days, report, None)
            except Exception:
                log.exception("Usage refresh failed")
                self.usage_result = (days, None, "Usage is unavailable. Check the log and refresh.")
        threading.Thread(target=read, name="usage-report", daemon=True).start()

    def render_usage(self):
        if not self.report:
            return
        report = self.report
        totals = report["totals"]
        for (_, value, _), key in zip(self.cards, ("total_tokens", "input_tokens", "cached_input_tokens", "output_tokens")):
            label = number(totals["counts"], key, totals["missing"])
            value.setStringValue_(label)
            value.setToolTip_(label)
        measured, calls = totals["measured"], totals["calls"]
        status = f"{measured} of {calls} calls have token totals · updated {time.strftime('%H:%M:%S', time.localtime(report['updated_at']))}"
        if report.get("skipped"):
            status += f" · {report['skipped']} records could not be recovered"
        self.usage_status.setStringValue_(status if calls else "No calls recorded in this period.")
        rows = []
        recent = self.grouping.selectedSegment() == 1
        for row in report["calls"] if recent else report["groups"]:
            purpose = usage.PURPOSES.get(row["purpose"], row["purpose"])
            counts = row["counts"]
            missing = None if recent else row["missing"]
            activity = f"{purpose} · {row['request_id'] or row['id']}" if recent else purpose
            rows.append({"activity": activity, "model": f"{row['provider']} · {row['model'] or 'Unknown model'}",
                         "calls": time.strftime("%m/%d %H:%M", time.localtime(row["started_at"])) if recent else str(row["calls"]),
                         "input": number(counts, "input_tokens", missing), "cache": number(counts, "cached_input_tokens", missing),
                         "output": number(counts, "output_tokens", missing), "total": number(counts, "total_tokens", missing),
                         "coverage": f"{row['coverage']} · {row['outcome']}" if recent else f"{row['measured']}/{row['calls']} measured"})
        self.attribution_data.rows = rows
        self.attribution_table.reloadData()
        linked = []
        for row in report["linked"]:
            snap = row["snapshot"]
            model = ", ".join(m.get("model") or "Unknown" for m in snap.get("models", []))
            linked.append({"name": row["name"], "provider": (snap.get("provider") or "Unknown") + (" · " + model if model else ""),
                           "tokens": number(snap.get("totals", {}), "total_tokens"),
                           "scope": "Native agents too" if snap.get("scope") == "including_native_children" else "Direct calls",
                           "status": ("Stale" if row["error"] else snap.get("status") or "Unavailable")
                                     + " · " + (snap.get("observed_at") or "not reported")})
        self.linked_data.rows = linked
        self.linked_table.reloadData()

    def tick(self):
        if self.usage_progress is not None:
            days, report = self.usage_progress
            self.usage_progress = None
            if days == self.days:
                self.report = report
                self.render_usage()
                self.usage_status.setStringValue_(self.usage_status.stringValue() + " · refreshing linked sessions…")
        if self.usage_result is not None:
            days, report, error = self.usage_result
            self.usage_result = None
            self.usage_busy = False
            self.refresh_usage_button.setEnabled_(True)
            self.usage_refresh_at = time.monotonic() + 30
            if days == self.days:
                if report:
                    self.report = report
                    self.render_usage()
                else:
                    self.usage_status.setStringValue_(error)
            if self.refresh_again or days != self.days:
                self.refresh_usage()
        if not self.window.isVisible():
            return
        if self.page == "usage":
            if not self.usage_busy and time.monotonic() >= self.usage_refresh_at:
                self.refresh_usage()
            return
        selected = state.read()
        self.switches[0].setState_(int(state.is_listening(selected)))
        self.switches[1].setState_(int(voice_control.read()["enabled"]))
        self.switches[2].setState_(int(self.app.mini.visible))
        self.capture_status.setStringValue_("Microphone " + state.describe(selected))
        saved = self.app.model_snapshot
        for lane, _, picker in self.model_rows:
            pair = saved["settings"][lane] if saved else None
            picker.setEnabled_(bool(saved) and not self.app.model_busy)
            picker.setTitle_((f"{pair['model'] or 'Provider default'} · {pair['effort'] or 'default'}  ▾") if pair else "Models unavailable")
        self.model_status.setStringValue_(getattr(self.app, "model_error", None) or
                                         ("Loading models…" if self.app.model_busy else
                                          "Changes apply to new requests. Existing work sessions keep their model."))

    def close(self):
        self.timer.invalidate()
        self.window.orderOut_(None)
        self.root.owner = None
        self.actions.owner = None
