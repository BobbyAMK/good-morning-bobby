"""Project punctuation rules for generated and externally supplied prose."""

import re

_URL = re.compile(r"https?://[^\s<>\[\]()`]+")
_DASHES = str.maketrans({"\u2013": "-", "\u2014": "-"})


def plain_dashes(text: str) -> str:
    """Use ASCII hyphens in prose; preserve URLs so source links still resolve."""
    parts: list[str] = []
    pos = 0
    for match in _URL.finditer(text):
        parts.append(text[pos : match.start()].translate(_DASHES))
        parts.append(match.group())
        pos = match.end()
    parts.append(text[pos:].translate(_DASHES))
    return "".join(parts)
