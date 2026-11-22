"""Undo targeting and edit guards without clipboard writes or posted input."""
import unittest
from unittest.mock import patch

from ramble.desktop_paste import DesktopPaste, PasteReceipt, Q, replacement_value


class DesktopUndoTests(unittest.TestCase):
    def setUp(self):
        self.desktop = DesktopPaste()
        self.target = (123, "original input")
        self.receipt = PasteReceipt(self.target, 10, "Pasted text")
        self.enterContext(patch.object(self.desktop, "permitted", return_value=True))
        self.enterContext(patch.object(self.desktop, "target", return_value=self.target))
        self.count = self.enterContext(patch.object(self.desktop, "keypress_count", return_value=10))
        self.snapshot = self.enterContext(patch.object(self.desktop, "text_state", return_value=("Pasted text", (11, 0))))
        self.post = self.enterContext(patch('ramble.desktop_paste.Q.CGEventPostToPid'))

    def test_undo_sends_only_command_z_to_original_application(self):
        self.assertTrue(self.desktop.undo(self.receipt)[0])
        self.assertEqual(len(self.post.call_args_list), 2)
        for call in self.post.call_args_list:
            self.assertEqual(call.args[0], 123)
            self.assertEqual(Q.CGEventGetIntegerValueField(call.args[1], Q.kCGKeyboardEventKeycode), 6)
            self.assertEqual(Q.CGEventGetFlags(call.args[1]), Q.kCGEventFlagMaskCommand)

    def test_new_keystrokes_block_undo_even_if_text_matches_again(self):
        self.count.return_value = 12
        self.assertFalse(self.desktop.undo(self.receipt)[0])
        self.post.assert_not_called()

    def test_changed_input_or_paste_not_applied_never_undo_other_work(self):
        with patch.object(self.desktop, 'target', return_value=(123, "another input")):
            self.assertFalse(self.desktop.undo(self.receipt)[0])
        for value in ("Text before paste", "Pasted text plus another edit"):
            self.snapshot.return_value = value, (0, 0)
            self.assertFalse(self.desktop.undo(self.receipt)[0])
        self.post.assert_not_called()

    def test_lost_permission_does_not_post_undo(self):
        with patch.object(self.desktop, 'permitted', return_value=False):
            self.assertFalse(self.desktop.undo(self.receipt)[0])
        self.post.assert_not_called()

    def test_receipt_handles_selection_replacement_and_emoji_offsets(self):
        self.snapshot.return_value = "Hi 😀 old", (6, 3)
        receipt = self.desktop.prepare_undo(self.target, "new")
        self.assertEqual(receipt.expected_value, "Hi 😀 new")
        self.assertEqual(receipt.keypress_count, 10)
        self.assertEqual(replacement_value("Hi 😀", (5, 0), "!"), "Hi 😀!")
        self.assertIsNone(replacement_value("Hi 😀", (4, 0), "!"))
        self.assertIsNone(replacement_value("Hi", (5, 0), "!"))

    def test_custom_controls_keep_focus_and_keyboard_checks_without_text_snapshot(self):
        self.snapshot.side_effect = ValueError("Unsupported accessibility value")
        receipt = self.desktop.prepare_undo(self.target, "Text")
        self.assertIsNone(receipt.expected_value)
        self.assertTrue(self.desktop.undo(receipt)[0])


if __name__ == '__main__':
    unittest.main()
