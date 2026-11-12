"""Content fingerprints for duplicate detection.

``text_sha256`` catches exact duplicates; a 64-bit SimHash over word 3-grams
catches near-duplicates (the same page with a different date stamp, tracking
banner or footer year). Two pages are near-duplicates when the Hamming
distance between their SimHashes is small (a few bits out of 64).
"""

from __future__ import annotations

import hashlib
import re

_WORD = re.compile(r"\w+", re.UNICODE)
SHINGLE_SIZE = 3


def text_sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def simhash(text: str) -> int:
    """64-bit SimHash as a signed integer (fits PostgreSQL BIGINT)."""
    words = _WORD.findall(text.lower())
    if not words:
        return 0
    shingles = [
        " ".join(words[i : i + SHINGLE_SIZE]) for i in range(max(1, len(words) - SHINGLE_SIZE + 1))
    ]
    weights = [0] * 64
    for shingle in shingles:
        value = int.from_bytes(hashlib.blake2b(shingle.encode(), digest_size=8).digest(), "big")
        for bit in range(64):
            weights[bit] += 1 if value >> bit & 1 else -1
    unsigned = sum(1 << bit for bit in range(64) if weights[bit] > 0)
    return unsigned - (1 << 64) if unsigned >= 1 << 63 else unsigned


def hamming_distance(a: int, b: int) -> int:
    return ((a ^ b) & (2**64 - 1)).bit_count()
