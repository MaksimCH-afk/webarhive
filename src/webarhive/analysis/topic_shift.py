"""Topic-shift detection (spec §6, etap 2).

Compares normalized title+description+h1 of consecutive versions:
- lowercase
- collapsed whitespace
- strip stop-words (RU + EN articles/prepositions/conjunctions)
- words sorted (so permutations are identical)

A version is "shifted" only if the count of differing *significant*
words (added + removed) STRICTLY EXCEEDS TITLE_SHIFT_THRESHOLD (default 2).

Goal: don't burn LLM calls on cosmetic edits (one word changed,
shuffled), but catch real semantic shifts.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# Compact stop-word set covering RU + EN function words.
# Editable here; kept lean to avoid over-stripping content keywords.
_STOP_WORDS = frozenset({
    # EN
    "a", "an", "the", "and", "or", "but", "of", "in", "on", "at", "for",
    "to", "with", "by", "from", "as", "is", "are", "was", "were", "be",
    "this", "that", "these", "those", "it", "its", "we", "you", "they",
    "your", "our", "their", "my", "his", "her",
    # RU
    "и", "в", "во", "не", "что", "он", "на", "я", "с", "со", "как",
    "а", "то", "все", "она", "так", "его", "но", "да", "ты", "к", "у",
    "же", "вы", "за", "бы", "по", "только", "ее", "мне", "было", "вот",
    "от", "меня", "о", "из", "ему", "теперь", "когда", "даже", "ну",
    "вдруг", "ли", "если", "уже", "или", "ни", "быть", "был", "него",
    "до", "вас", "нибудь", "опять", "уж", "вам", "ведь", "там", "потом",
    "себя", "ничего", "ей", "может", "они", "тут", "где", "есть", "надо",
    "ней", "для", "мы", "тебя", "их", "чем", "была", "сам", "чтоб", "без",
    "будто", "чего", "раз", "тоже", "себе", "под", "будет", "ж", "тогда",
    "кто", "этот", "того", "потому", "этого", "какой", "совсем", "ним",
    "здесь", "этом", "один", "почти", "мой", "тем", "чтобы", "нее",
})

_WORD_RE = re.compile(r"[\w\-]+", re.UNICODE)


def _tokenize(text: str) -> list[str]:
    if not text:
        return []
    text = text.lower()
    raw = _WORD_RE.findall(text)
    return [w for w in raw if w and w not in _STOP_WORDS and len(w) > 1]


@dataclass(frozen=True, slots=True)
class VersionFingerprint:
    """Normalized + sorted significant words from title+description+h1.

    Equality + size-diff of two fingerprints drives shift detection.
    """
    words: tuple[str, ...]

    @classmethod
    def from_fields(cls, title: str, description: str, h1: str) -> VersionFingerprint:
        tokens = _tokenize(" ".join((title or "", description or "", h1 or "")))
        return cls(tuple(sorted(tokens)))

    def diff_count(self, other: VersionFingerprint) -> int:
        """Number of words present in exactly one of the two fingerprints
        (treats inputs as sets — permutation is invariant)."""
        a, b = set(self.words), set(other.words)
        return len(a.symmetric_difference(b))


def is_shift(
    a: VersionFingerprint,
    b: VersionFingerprint,
    *,
    threshold: int,
) -> bool:
    """True only if number of differing significant words STRICTLY exceeds
    the threshold (spec §6, etap 2)."""
    return a.diff_count(b) > threshold
