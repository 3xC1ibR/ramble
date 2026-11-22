"""Clipboard delivery and focus-checked paste on the AppKit main thread."""
from dataclasses import dataclass

import AppKit as A
import ApplicationServices as AX
import Quartz as Q


@dataclass(frozen=True)
class PasteReceipt:
    target: object
    keypress_count: int
    expected_value: str | None


def replacement_value(value, selection, text):
    """Accessibility text offsets count UTF-16 code units, including emoji."""
    encoded = value.encode('utf-16-le')
    start, length = selection
    end = start + length
    if start < 0 or length < 0 or end * 2 > len(encoded):
        return None
    try:
        return (encoded[:start * 2] + text.encode('utf-16-le') + encoded[end * 2:]).decode('utf-16-le')
    except UnicodeError:
        return None


class DesktopPaste:
    @staticmethod
    def keypress_count():
        # Read only the count, never key contents. Our private synthetic event
        # source does not contribute to the hardware keyboard's counter.
        return Q.CGEventSourceCounterForEventType(Q.kCGEventSourceStateHIDSystemState, Q.kCGEventKeyDown)

    @staticmethod
    def text_state(target):
        if target is None:
            return None
        element = target[1]
        AX.AXUIElementSetMessagingTimeout(element, .2)
        error, value = AX.AXUIElementCopyAttributeValue(element, AX.kAXValueAttribute, None)
        if error or not isinstance(value, str):
            return None
        error, selection = AX.AXUIElementCopyAttributeValue(element, AX.kAXSelectedTextRangeAttribute, None)
        if error or selection is None:
            return None
        valid, selected_range = AX.AXValueGetValue(selection, AX.kAXValueCFRangeType, None)
        return (value, tuple(selected_range)) if valid else None

    def prepare_undo(self, target, text):
        try:
            snapshot = self.text_state(target)
        except Exception:
            # A custom control's incomplete AX support must not block paste.
            snapshot = None
        expected = replacement_value(*snapshot, text) if snapshot else None
        return PasteReceipt(target, self.keypress_count(), expected)

    def undo_unchanged(self, receipt):
        return self.keypress_count() == receipt.keypress_count

    def undo(self, receipt):
        if not self.permitted() or receipt.target is None or self.target() != receipt.target:
            return False, "Return to the original input and use its Undo command."
        if not self.undo_unchanged(receipt):
            return False, "The input received another keystroke. Use the app's Undo command."
        if receipt.expected_value is not None:
            snapshot = self.text_state(receipt.target)
            if snapshot is None or snapshot[0] != receipt.expected_value:
                return False, "The text changed after dictation. Use the app's Undo command."
        if not self.post_command(receipt.target, 6):  # ANSI Z
            return False, "Undo could not be sent. Use Command+Z in the original input."
        return True, "Undo sent to the input where the dictation was pasted."

    @staticmethod
    def permitted():
        return bool(AX.AXIsProcessTrusted())

    @staticmethod
    def request_permission():
        AX.AXIsProcessTrustedWithOptions({AX.kAXTrustedCheckOptionPrompt: True})

    def target(self):
        app = A.NSWorkspace.sharedWorkspace().frontmostApplication()
        if not app or not self.permitted():
            return None
        pid = app.processIdentifier()
        element = AX.AXUIElementCreateApplication(pid)
        AX.AXUIElementSetMessagingTimeout(element, .2)
        error, focus = AX.AXUIElementCopyAttributeValue(element, AX.kAXFocusedUIElementAttribute, None)
        return (pid, focus) if not error and focus else None

    @staticmethod
    def copy(text):
        clipboard = A.NSPasteboard.generalPasteboard()
        clipboard.clearContents()
        if not clipboard.setString_forType_(text, A.NSPasteboardTypeString):
            raise RuntimeError("Could not copy dictation to the clipboard")

    def paste(self, target):
        if not self.permitted():
            return False, "Copied. Enable Ramble in System Settings → Privacy & Security → Accessibility for automatic paste."
        if target is None or self.target() != target:
            return False, "Copied. The selected input changed or could not be verified; press Command+V to paste."
        if not self.post_command(target, 9):  # ANSI V
            return False, "Copied. Automatic paste is unavailable; press Command+V to paste."
        return True, "Dictation copied and sent to the selected input."

    @staticmethod
    def post_command(target, keycode):
        # A separate source prevents held shortcut modifiers from contaminating ⌘V.
        source = Q.CGEventSourceCreate(Q.kCGEventSourceStatePrivate)
        events = [Q.CGEventCreateKeyboardEvent(source, keycode, down) for down in (True, False)]
        if any(event is None for event in events):
            return False
        for event in events:
            Q.CGEventSetFlags(event, Q.kCGEventFlagMaskCommand)
            Q.CGEventPostToPid(target[0], event)
        return True
