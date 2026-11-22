"""Configurable Carbon global shortcut; no global keyboard monitoring."""
from __future__ import annotations

import ctypes as C
import json
import logging

from . import config

log = logging.getLogger("ramble.shortcuts")
DEFAULT_SHORTCUT = "Control+Option+Space"
MODIFIERS = {"control": 4096, "option": 2048, "shift": 512, "command": 256}
ALIASES = {"cmd": "command", "alt": "option", "ctrl": "control"}
KEYS = {
    "A": 0, "S": 1, "D": 2, "F": 3, "H": 4, "G": 5, "Z": 6, "X": 7, "C": 8, "V": 9, "B": 11,
    "Q": 12, "W": 13, "E": 14, "R": 15, "Y": 16, "T": 17,
    "1": 18, "2": 19, "3": 20, "4": 21, "6": 22, "5": 23, "=": 24, "9": 25,
    "7": 26, "-": 27, "8": 28, "0": 29, "]": 30, "O": 31, "U": 32, "[": 33, "I": 34, "P": 35,
    "L": 37, "J": 38, "'": 39, "K": 40, ";": 41, "\\": 42, ",": 43, "/": 44, "N": 45, "M": 46, ".": 47,
}
KEYS.update(SPACE=49, RETURN=36, TAB=48, F1=122, F2=120, F3=99, F4=118,
            F5=96, F6=97, F7=98, F8=100, F9=101, F10=109, F11=103, F12=111)


def parse_shortcut(value):
    if not isinstance(value, str):
        raise ValueError("The dictation shortcut must be text, such as Control+Option+Space.")
    parts = [part.strip().lower() for part in value.split('+')]
    if len(parts) < 2 or parts[-1].upper() not in KEYS:
        raise ValueError("Use a modifier and key, such as Control+Option+Space.")
    modifiers = [ALIASES.get(part, part) for part in parts[:-1]]
    if any(part not in MODIFIERS for part in modifiers) or len(set(modifiers)) != len(modifiers):
        raise ValueError("Modifiers are Control, Option, Shift and Command; use each once.")
    if not set(modifiers) & {"control", "option", "command"}:
        raise ValueError("Include Control, Option or Command.")
    key = parts[-1].upper()
    name = "+".join([m.title() for m in MODIFIERS if m in modifiers] +
                    [key.title() if key in {"SPACE", "RETURN", "TAB"} else key])
    return KEYS[key], sum(MODIFIERS[m] for m in modifiers), name


def load_shortcut():
    try:
        return parse_shortcut(json.loads((config.HOME / "dictation.json").read_text())["shortcut"])[2]
    except FileNotFoundError:
        return DEFAULT_SHORTCUT


class EventType(C.Structure):
    _fields_ = [("eventClass", C.c_uint32), ("eventKind", C.c_uint32)]


class HotKeyID(C.Structure):
    _fields_ = [("signature", C.c_uint32), ("id", C.c_uint32)]


HANDLER = C.CFUNCTYPE(C.c_int32, C.c_void_p, C.c_void_p, C.c_void_p)
SIGNATURE = int.from_bytes(b"Rmbl", "big")
ESCAPE_KEY_CODE = 53


class GlobalShortcut:
    def __init__(self, callback, *, cancel=None):
        self.callback = callback
        self.cancel = cancel
        self.cancel_reference = C.c_void_p()
        self.cancel_identifier = 0x80000000
        self.cancel_pressed = False
        self.reference = C.c_void_p()
        self.handler = C.c_void_p()
        self.value = None
        self.sequence = 0
        self.pressed = False
        self.library = C.CDLL("/System/Library/Frameworks/Carbon.framework/Carbon")
        signatures = {
            "GetApplicationEventTarget": (C.c_void_p, []),
            "InstallEventHandler": (C.c_int32, [C.c_void_p, HANDLER, C.c_uint32, C.POINTER(EventType), C.c_void_p, C.POINTER(C.c_void_p)]),
            "RegisterEventHotKey": (C.c_int32, [C.c_uint32, C.c_uint32, HotKeyID, C.c_void_p, C.c_uint32, C.POINTER(C.c_void_p)]),
            "UnregisterEventHotKey": (C.c_int32, [C.c_void_p]),
            "RemoveEventHandler": (C.c_int32, [C.c_void_p]),
            "GetEventKind": (C.c_uint32, [C.c_void_p]),
            "GetEventParameter": (C.c_int32, [C.c_void_p, C.c_uint32, C.c_uint32, C.c_void_p, C.c_uint32, C.c_void_p, C.c_void_p]),
        }
        for name, (result, arguments) in signatures.items():
            function = getattr(self.library, name)
            function.restype, function.argtypes = result, arguments
        self.handler_function = HANDLER(self._event)
        events = (EventType * 2)(EventType(int.from_bytes(b"keyb", "big"), 5),
                                 EventType(int.from_bytes(b"keyb", "big"), 6))
        result = self.library.InstallEventHandler(self.library.GetApplicationEventTarget(), self.handler_function,
                                                  2, events, None, C.byref(self.handler))
        if result:
            raise RuntimeError(f"Could not install shortcut handler ({result}).")

    def _event(self, call, event, context):
        identifier = HotKeyID()
        result = self.library.GetEventParameter(event, int.from_bytes(b"----", "big"),
                                                int.from_bytes(b"hkid", "big"), None,
                                                C.sizeof(identifier), None, C.byref(identifier))
        if result or identifier.signature != SIGNATURE:
            return -9874  # eventNotHandledErr
        if identifier.id == self.cancel_identifier and self.cancel_reference:
            if self.library.GetEventKind(event) == 6:
                self.cancel_pressed = False
            elif not self.cancel_pressed:
                self.cancel_pressed = True
                try:
                    self.cancel()
                except Exception:
                    log.exception("Dictation cancellation failed")
            return 0
        if identifier.id != self.sequence:
            return -9874
        if self.library.GetEventKind(event) == 6:
            self.pressed = False
        elif not self.pressed:
            self.pressed = True
            try:
                self.callback()
            except Exception:
                log.exception("Dictation shortcut failed")
        return 0

    def set_cancel_active(self, active):
        """Reserve Escape only while dictating or awaiting the final transcript."""
        if bool(self.cancel_reference) == bool(active):
            return
        if not active:
            self.library.UnregisterEventHotKey(self.cancel_reference)
            self.cancel_reference = C.c_void_p()
            self.cancel_pressed = False
            return
        if self.cancel is None:
            raise ValueError("The dictation cancellation handler is unavailable.")
        reference = C.c_void_p()
        identifier = self.cancel_identifier + 1
        result = self.library.RegisterEventHotKey(ESCAPE_KEY_CODE, 0, HotKeyID(SIGNATURE, identifier),
                                                 self.library.GetApplicationEventTarget(), 0, C.byref(reference))
        if result:
            raise ValueError(f"Escape is reserved by another application ({result}). Release it before starting dictation.")
        self.cancel_reference = reference
        self.cancel_identifier = identifier
        self.cancel_pressed = False

    def register(self, value):
        key, modifiers, normalized = parse_shortcut(value)
        if normalized == self.value:
            return normalized
        reference = C.c_void_p()
        result = self.library.RegisterEventHotKey(key, modifiers, HotKeyID(SIGNATURE, self.sequence + 1),
                                                 self.library.GetApplicationEventTarget(), 0, C.byref(reference))
        if result:
            raise ValueError(f"That shortcut is unavailable ({result}). Choose a different combination.")
        if self.reference:
            self.library.UnregisterEventHotKey(self.reference)
        self.reference, self.value = reference, normalized
        self.sequence += 1
        self.pressed = False
        return normalized

    def save(self, value):
        previous = self.value
        normalized = self.register(value)
        try:
            config.HOME.mkdir(parents=True, exist_ok=True)
            temporary = config.HOME / ".dictation.json.tmp"
            temporary.write_text(json.dumps({"shortcut": normalized}) + "\n")
            temporary.replace(config.HOME / "dictation.json")
        except OSError:
            if previous:
                self.register(previous)
            else:
                self.library.UnregisterEventHotKey(self.reference)
                self.reference, self.value = C.c_void_p(), None
            raise
        return normalized

    def close(self):
        self.set_cancel_active(False)
        if self.reference:
            self.library.UnregisterEventHotKey(self.reference)
            self.reference = C.c_void_p()
        if self.handler:
            self.library.RemoveEventHandler(self.handler)
            self.handler = C.c_void_p()
