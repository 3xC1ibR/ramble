"""Shared address recognition for transcription checks and the fast lane."""

import re

# Whisper's observed spellings of the name.
NAME = re.compile(r"\b(?:hey|hi|ok|okay|yo)?[\s,]*\b(ramble|rambo|rumble|rambel|rambles|bramble|gramble|grable|granville|rambl)\b[\s,.!?]*", re.I)
# Distant mishearings require opening address punctuation.
ADDRESS = re.compile(r"^(?:(?:hey|hi|okay|ok)[\s,]+)?(?:rabble|crabble|grambles|scramble|mr\.?\s+abel)[,.!?]+\s*", re.I)


def has_name(text: str) -> bool:
    return bool(NAME.search(text) or ADDRESS.search(text))
