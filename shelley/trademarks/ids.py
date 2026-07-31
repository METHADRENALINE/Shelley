from __future__ import annotations

import re
import secrets
from collections.abc import Callable

TRADEMARK_ID_ALPHABET = "ABCDEFGHJKMNPQRSTVWXYZ23456789"
TRADEMARK_ID_PATTERN = re.compile(
    r"^[ABCDEFGHJKMNPQRSTVWXYZ23456789]{5}"
    r"(?:-[ABCDEFGHJKMNPQRSTVWXYZ23456789]{5}){4}$"
)


def generate_trademark_id(
    randbelow: Callable[[int], int] = secrets.randbelow,
) -> str:
    groups = []
    for _ in range(5):
        groups.append("".join(TRADEMARK_ID_ALPHABET[randbelow(len(TRADEMARK_ID_ALPHABET))] for _ in range(5)))
    return "-".join(groups)


def is_trademark_id(value: str) -> bool:
    return TRADEMARK_ID_PATTERN.fullmatch(str(value).strip().upper()) is not None


def normalize_trademark_id(value: str) -> str:
    normalized = str(value).strip().upper()
    if not is_trademark_id(normalized):
        raise ValueError("Invalid trademark ID")
    return normalized
