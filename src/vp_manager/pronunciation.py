"""Non-cascading dictionary matches used for QA expectations and promotion evidence."""

from __future__ import annotations

import re


def matched_entries(text: str, entries: list[dict]) -> list[dict]:
    """Use whole ASCII tokens; surrounding Japanese characters are valid boundaries.

    These are eligible dictionary spans, not proof of the engine's internal lookup.
    A longer dictionary surface masks a contained shorter surface.
    """
    by_surface = {entry["sur"]: entry for entry in entries}
    if not by_surface:
        return []
    pattern = re.compile("|".join(re.escape(word) for word in sorted(by_surface, key=len, reverse=True)))
    result = []
    ascii_token = re.compile(r"[A-Za-z0-9_]")
    for match in pattern.finditer(text):
        word = match.group()
        start, end = match.span()
        if ascii_token.fullmatch(word[0]) and start and ascii_token.fullmatch(text[start - 1]):
            continue
        if ascii_token.fullmatch(word[-1]) and end < len(text) and ascii_token.fullmatch(text[end]):
            continue
        result.append({"start": start, "end": end, "entry": by_surface[word]})
    return result


def expected_reading(text: str, entries: list[dict]) -> str:
    output, offset = [], 0
    for match in matched_entries(text, entries):
        output.extend((text[offset : match["start"]], match["entry"]["pron"]))
        offset = match["end"]
    output.append(text[offset:])
    return "".join(output)
