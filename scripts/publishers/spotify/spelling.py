"""Conservative comparisons for catalogue titles and individual artist names."""

from __future__ import annotations

import re
import unicodedata


LATIN_OPTIONAL_MARKS = frozenset(
    "\u0300\u0301\u0302\u0303\u0304\u0306\u0307\u0308\u030a\u030b\u030c\u0327\u0328"
)
ARABIC_VOWEL_MARKS = frozenset(chr(code) for code in range(0x064B, 0x0651))
HEBREW_VOWEL_MARKS = frozenset(chr(code) for code in range(0x05B0, 0x05BC)) | {"\u05c7"}
SPACED_AMPERSAND = re.compile(r"(?<=\S) +& +(?=\S)")


def normalize_recording_evidence_text(value: str) -> str:
    # Use case mapping while retaining distinctions such as ß, ss and combining marks.
    text = unicodedata.normalize("NFC", value).lower().replace("ς", "σ")
    return " ".join(unicodedata.normalize("NFC", text).split())


def conjunction_difference(left: str, right: str) -> bool:
    left, right = (normalize_recording_evidence_text(value) for value in (left, right))
    return left != right and SPACED_AMPERSAND.sub(" and ", left) == SPACED_AMPERSAND.sub(" and ", right)


def spelling_difference(left: str, right: str) -> bool:
    """Recognize only supported letters or one-sided mark omissions at each position."""
    left, right = (normalize_recording_evidence_text(value) for value in (left, right))
    if not left or not right or left == right:
        return False
    left_letters, right_letters = letters_and_marks(left), letters_and_marks(right)
    return len(left_letters) == len(right_letters) and all(
        compatible_letters(left_letter, right_letter)
        for left_letter, right_letter in zip(left_letters, right_letters)
    )


def letters_and_marks(value: str) -> list[tuple[str, str]]:
    letters: list[tuple[str, str]] = []
    for character in unicodedata.normalize("NFD", value):
        if letters and unicodedata.category(character).startswith("M"):
            base, marks = letters[-1]
            letters[-1] = (base, marks + character)
        else:
            letters.append((character, ""))
    return letters


def compatible_letters(left: tuple[str, str], right: tuple[str, str]) -> bool:
    base, marks = left
    other_base, other_marks = right
    if base != other_base:
        return {base, other_base} == {"i", "ı"} and {marks, other_marks} <= {"", "\u0307"}
    if marks == other_marks:
        return True
    # One spelling can omit allowed marks. Two different fully marked letters stay distinct.
    shorter, longer = sorted((marks, other_marks), key=len)
    missing = list(longer)
    for mark in shorter:
        if mark not in missing:
            return False
        missing.remove(mark)
    return bool(missing) and set(missing) <= allowed_marks(base)


def allowed_marks(base: str) -> frozenset[str]:
    name = unicodedata.name(base, "")
    if name.startswith("LATIN "):
        return LATIN_OPTIONAL_MARKS
    if name.startswith("GREEK "):
        return frozenset("\u0301")
    if base == "е":
        return frozenset("\u0308")
    if name.startswith("ARABIC LETTER "):
        return ARABIC_VOWEL_MARKS
    if name.startswith("HEBREW LETTER "):
        return HEBREW_VOWEL_MARKS
    return frozenset()
