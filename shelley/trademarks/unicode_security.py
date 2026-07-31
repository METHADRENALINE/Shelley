from __future__ import annotations

import unicodedata
from functools import lru_cache
from importlib import resources

CONFUSABLES_RESOURCE = "confusables-17.0.0.txt"
PUNCTUATION_EQUIVALENTS = str.maketrans(
    {
        "\u002d": "-",
        "\u02d7": "-",
        "\u058a": "-",
        "\u05be": "-",
        "\u1806": "-",
        "\u2010": "-",
        "\u2011": "-",
        "\u2012": "-",
        "\u2013": "-",
        "\u2014": "-",
        "\u2015": "-",
        "\u2043": "-",
        "\u207b": "-",
        "\u208b": "-",
        "\u2212": "-",
        "\u2e3a": "-",
        "\u2e3b": "-",
        "\ufe58": "-",
        "\ufe63": "-",
        "\uff0d": "-",
        "\u0027": "'",
        "\u02b9": "'",
        "\u02bb": "'",
        "\u02bc": "'",
        "\u02bd": "'",
        "\u055a": "'",
        "\u05f3": "'",
        "\u07f4": "'",
        "\u07f5": "'",
        "\u2018": "'",
        "\u2019": "'",
        "\u201a": "'",
        "\u201b": "'",
        "\u2032": "'",
        "\u2035": "'",
        "\u275b": "'",
        "\u275c": "'",
        "\ua78c": "'",
        "\uff07": "'",
        "\u0022": '"',
        "\u02ba": '"',
        "\u05f4": '"',
        "\u201c": '"',
        "\u201d": '"',
        "\u201e": '"',
        "\u201f": '"',
        "\u2033": '"',
        "\u2036": '"',
        "\u275d": '"',
        "\u275e": '"',
        "\uff02": '"',
    }
)


@lru_cache(maxsize=1)
def confusable_mappings() -> dict[str, str]:
    content = resources.files("shelley.trademarks.data").joinpath(CONFUSABLES_RESOURCE).read_text(encoding="utf-8")
    mappings: dict[str, str] = {}
    for line in content.splitlines():
        data = line.partition("#")[0].strip()
        if not data:
            continue
        source_field, target_field, _mapping_type = data.split(";", maxsplit=2)
        source = "".join(chr(int(codepoint, 16)) for codepoint in source_field.split())
        if len(source) != 1:
            raise RuntimeError("Unsupported Unicode confusable source sequence")
        mappings[source] = "".join(chr(int(codepoint, 16)) for codepoint in target_field.split())
    return mappings


def confusable_skeleton(value: str) -> str:
    mappings = confusable_mappings()
    canonical = unicodedata.normalize("NFKC", str(value)).translate(PUNCTUATION_EQUIVALENTS)
    folded = unicodedata.normalize("NFD", canonical.casefold())
    mapped = "".join(mappings.get(character, character) for character in folded)
    return unicodedata.normalize("NFD", mapped.casefold())
