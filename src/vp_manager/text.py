"""Lossless source units, conservative reading candidates and synthesis chunks."""

import hashlib
import re
import unicodedata
from copy import deepcopy

from .common import VPError

SPLIT_SCHEMA_VERSION = 1
ENGINE_PREPARATION_VERSION = 1
_ENGINE_QUOTE_TRANSLATION = str.maketrans({char: " " for char in "「」『』"})
_LATIN = re.compile(r"[A-Za-z0-9Ａ-Ｚａ-ｚ０-９]+(?:[._/@:+#%&=\\-][A-Za-z0-9Ａ-Ｚａ-ｚ０-９]+)*[+#%®™]*")
_NUM_UNIT = re.compile(
    r"[+−-]?[0-9０-９]+(?:[.,．][0-9０-９]+)*(?:\s?(?:GHz|MHz|kHz|Hz|GiB|MiB|KiB|GB|MB|KB|ms|mm|cm|km|kg|mL|ml|mg|秒|分|時間|日|年|月|個|件|枚|文字|倍|度|円|人|回|台|本|％|%|℃|°C|[smgVWA]))?"
)
_SYMBOL = re.compile(r"[._/@:+#%&=\\−\-®™％℃°<>\[\]{}]")
_JP_SYMBOL = re.compile(r"[一-龯ァ-ヺー]+[+/#@®™]+[一-龯ァ-ヺーA-Za-z0-9]*")


def ingest_text(text: str) -> list[dict]:
    """Keep line terminators in source units so source reconstruction is exact."""
    if not isinstance(text, str):
        raise VPError("Text input must be a string")
    return [
        {"id": f"u{i:04d}", "source_text": line, "slide_id": None, "paragraph_index": i}
        for i, line in enumerate(text.splitlines(keepends=True), 1)
    ]


def actual_spoken_text(unit: dict) -> str:
    """Read the prepared synthesis copy; controls never carry narration.

    Call ``prepare_synthesis_units`` before using this selector on source units.
    A missing engine copy falls back to the unchanged user reading or source.
    """
    if unit.get("note_control"):
        return ""
    text = unit.get("engine_text", unit.get("spoken_text", unit["source_text"]))
    if not isinstance(text, str):
        raise VPError("Synthesis text must be a string")
    return text


def prepare_synthesis_units(units: list[dict]) -> list[dict]:
    """Replace only Japanese quote delimiters in an audited, separate engine copy.

    The original source and user reading remain byte-for-byte unchanged. Audit
    offsets are code points in the user reading (or source when no reading was
    supplied), so they must not be interpreted as original-source decision spans.
    Recompute from that input every time rather than trusting older engine keys.
    """
    prepared = deepcopy(units)
    for unit in prepared:
        unit.pop("engine_text", None)
        unit.pop("engine_preparation", None)
        if unit.get("note_control"):
            continue
        original = actual_spoken_text(unit)
        replacements = [
            {"index": index, "source": char, "replacement": " "}
            for index, char in enumerate(original)
            if char in "「」『』"
        ]
        if replacements:
            output = original.translate(_ENGINE_QUOTE_TRANSLATION)
            unit["engine_text"] = output
            unit["engine_preparation"] = {
                "schema_version": ENGINE_PREPARATION_VERSION,
                "replacements": replacements,
                "input_sha256": hashlib.sha256(original.encode("utf-8")).hexdigest(),
                "output_sha256": hashlib.sha256(output.encode("utf-8")).hexdigest(),
            }
    return prepared


def has_risky_symbols(surface: str) -> bool:
    return bool(_SYMBOL.search(surface))


def is_unicode_boundary(text: str, index: int) -> bool:
    if index <= 0 or index >= len(text):
        return True
    char = text[index]
    return not (
        unicodedata.combining(char)
        or unicodedata.category(char) in {"Mn", "Mc", "Me"}
        or char == "\u200d"
        or text[index - 1] == "\u200d"
        or 0x1F3FB <= ord(char) <= 0x1F3FF
    )


def detect_candidates(units: list[dict], dictionary: list[dict]) -> list[dict]:
    """Flag review candidates; user-dictionary absence says nothing about engine knowledge."""
    registered = {entry["sur"]: entry for entry in dictionary if isinstance(entry.get("sur"), str)}
    result = []
    for unit in units:
        if unit.get("note_control"):
            continue
        source = unit["source_text"]
        spans = []
        for pattern, kind in ((_LATIN, "latin"), (_NUM_UNIT, "number_unit"), (_JP_SYMBOL, "symbol")):
            spans.extend((m.start(), m.end(), kind) for m in pattern.finditer(source))
        # Also flag punctuation-only constructs (e.g. SSML angle brackets).
        spans.extend(
            (m.start(), m.end(), "symbol") for m in re.finditer(r"<[^>\n]+>|[=+/#@\\{}\[\]]+", source)
        )
        for word in registered:
            if word:
                spans.extend((m.start(), m.end(), "dictionary") for m in re.finditer(re.escape(word), source))
        # Longest containing span wins. Partially overlapping matches merge so
        # a registered substring cannot hide a symbol in the surrounding term.
        merged = []
        for start, end, kind in sorted(spans, key=lambda x: (x[0], -(x[1] - x[0]))):
            if merged and start < merged[-1][1]:
                previous = merged[-1]
                merged[-1] = (previous[0], max(end, previous[1]), previous[2])
            else:
                merged.append((start, end, kind))
        for start, end, kind in merged:
            surface = source[start:end]
            if has_risky_symbols(surface):
                kind = "symbol"
            elif _NUM_UNIT.fullmatch(surface):
                kind = "number_unit"
            entry = registered.get(surface)
            candidate = {
                "id": f"candidate{len(result) + 1:04d}",
                "unit_id": unit["id"],
                "start": start,
                "end": end,
                "surface": surface,
                "kind": kind,
                "registered": entry is not None,
            }
            if entry and isinstance(entry.get("pron"), str):
                candidate["reading"] = entry["pron"]
            result.append(candidate)
    return result


def _atomic_spans(text: str, protected: list[str]) -> list[tuple[int, int]]:
    spans = [(m.start(), m.end()) for pattern in (_LATIN, _NUM_UNIT) for m in pattern.finditer(text)]
    for word in protected:
        if not isinstance(word, str) or not word:
            raise VPError("Protected terms must be nonempty strings")
        spans.extend((m.start(), m.end()) for m in re.finditer(re.escape(word), text))
    merged = []
    for start, end in sorted(spans):
        if merged and start < merged[-1][1]:
            merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
        else:
            merged.append((start, end))
    return merged


def split_text(text: str, limit: int = 140, protected: list[str] | None = None) -> list[str]:
    """Split by code points after reading substitution, without losing any text."""
    if not isinstance(text, str) or type(limit) is not int or limit < 1 or limit > 140:
        raise VPError("Text must be a string and limit an integer between 1 and 140")
    spans = _atomic_spans(text, protected or [])
    if any(end - start > limit for start, end in spans):
        raise VPError(
            "An indivisible term exceeds the synthesis text limit; provide a reading or explicit boundary",
            code="needs_decision",
        )
    forbidden = {index for start, end in spans for index in range(start + 1, end)}
    # Preserve combining sequences, emoji variation selectors and ZWJ sequences.
    for index in range(1, len(text)):
        if not is_unicode_boundary(text, index):
            forbidden.add(index)
    # Keep matched bracket groups intact when they fit into one chunk.
    stack = []
    pairs = {")": "(", "）": "（", "」": "「", "』": "『", "]": "[", "】": "【"}
    for index, char in enumerate(text):
        if char in pairs.values():
            stack.append((char, index))
        elif char in pairs and stack and stack[-1][0] == pairs[char]:
            _, start = stack.pop()
            if index + 1 - start <= limit:
                forbidden.update(range(start + 1, index + 1))
    chunks = []
    start = 0
    while start < len(text):
        ceiling = min(start + limit, len(text))
        if ceiling == len(text):
            chunks.append(text[start:])
            break
        choices = [end for end in range(start + 1, ceiling + 1) if end not in forbidden]
        if not choices:
            raise VPError("No safe text boundary within the synthesis limit", code="needs_decision")
        sentence = [
            end
            for end in choices
            if text[end - 1] in "。！？!?\n\r"
            or (text[end - 1] == "." and (end == len(text) or text[end].isspace()))
        ]
        clause = [end for end in choices if text[end - 1] in "、，,;；:：" or text[end - 1].isspace()]
        end = (sentence or clause or choices)[-1]
        chunks.append(text[start:end])
        start = end
    return chunks


def plan_chunks(units: list[dict], limit: int = 140, protected: list[str] | None = None) -> list[dict]:
    """Plan synthesis from the reading copy, retaining slide and paragraph identity."""
    chunks = []
    normalized_protected = [
        word.translate(_ENGINE_QUOTE_TRANSLATION) if isinstance(word, str) else word
        for word in (protected or [])
    ]
    for unit in prepare_synthesis_units(units):
        if unit.get("note_control"):
            continue
        spoken = actual_spoken_text(unit).rstrip("\r\n")
        if not spoken.strip():
            continue
        # PPTX soft line breaks become chunk boundaries; tabs become spaces in
        # the synthesis copy only. The exact source/decision copy is retained.
        for line in spoken.splitlines():
            for part in split_text(line.replace("\t", " "), limit, normalized_protected):
                if part.strip():
                    chunks.append(
                        {
                            "id": f"c{len(chunks) + 1:04d}",
                            "unit_id": unit["id"],
                            "slide_id": unit.get("slide_id"),
                            "paragraph_index": unit["paragraph_index"],
                            "text": part,
                        }
                    )
    return chunks
