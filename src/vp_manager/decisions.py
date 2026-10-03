"""Validate host decisions as data bound to an immutable source revision."""

from copy import deepcopy
from itertools import pairwise

from .common import VPError
from .text import has_risky_symbols, is_unicode_boundary


def _reading_valid(text: str) -> bool:
    punctuation = " 、。，．！？!?・ー〜～…‥「」『』（ ）()、,.;:：；"
    return (
        bool(text.strip())
        and any("ぁ" <= c <= "ゖ" or "ァ" <= c <= "ヺ" for c in text)
        and all(
            "ぁ" <= char <= "ゖ" or "ァ" <= char <= "ヺ" or char in "ゝゞヽヾ゙゚" or char in punctuation
            for char in text
        )
    )


def apply_decisions(units: list[dict], candidates: list[dict], source_revision: str, payload: dict) -> dict:
    if (
        not isinstance(payload, dict)
        or type(payload.get("schema_version")) is not int
        or payload["schema_version"] != 1
    ):
        raise VPError("Decision schema_version must be 1")
    allowed = {"schema_version", "source_revision", "overrides", "dictionary_entries", "accept_candidates"}
    if set(payload) - allowed:
        raise VPError("Unknown decision payload fields")
    if not isinstance(source_revision, str) or payload.get("source_revision") != source_revision:
        raise VPError("Decisions refer to a stale source revision")
    by_unit = {unit["id"]: unit for unit in units}
    if len(by_unit) != len(units):
        raise VPError("Duplicate source unit IDs")
    overrides = payload.get("overrides", [])
    entries = payload.get("dictionary_entries", [])
    accepted = payload.get("accept_candidates", [])
    if not all(isinstance(value, list) for value in (overrides, entries, accepted)):
        raise VPError("overrides, dictionary_entries and accept_candidates must be arrays")
    by_candidate = {candidate["id"]: candidate for candidate in candidates}
    if any(not isinstance(value, str) for value in accepted) or len(set(accepted)) != len(accepted):
        raise VPError("Accepted candidate IDs must be unique strings")
    for candidate_id in accepted:
        candidate = by_candidate.get(candidate_id)
        if candidate is None:
            raise VPError(f"Unknown candidate: {candidate_id}")
        if candidate["kind"] == "symbol" or has_risky_symbols(candidate["surface"]):
            raise VPError("Symbol-containing candidates require a full reading replacement")
    grouped = {unit_id: [] for unit_id in by_unit}
    for override in overrides:
        if not isinstance(override, dict) or set(override) != {
            "unit_id",
            "start",
            "end",
            "expected",
            "reading",
        }:
            raise VPError("Each override needs exactly unit_id, start, end, expected and reading")
        unit_id, start, end = override["unit_id"], override["start"], override["end"]
        if (
            not isinstance(unit_id, str)
            or unit_id not in by_unit
            or type(start) is not int
            or type(end) is not int
        ):
            raise VPError("Invalid override unit or source offsets")
        source = by_unit[unit_id]["source_text"]
        if not 0 <= start < end <= len(source) or source[start:end] != override["expected"]:
            raise VPError("Override expected text does not match its exact source span")
        if any(not is_unicode_boundary(source, index) for index in (start, end)):
            raise VPError("Override splits a Unicode character sequence")
        if not isinstance(override["reading"], str) or not _reading_valid(override["reading"]):
            raise VPError("Override reading must contain Japanese kana and ordinary punctuation only")
        grouped[unit_id].append(override)
    changed = deepcopy(units)
    for unit in changed:
        ordered = sorted(grouped[unit["id"]], key=lambda value: value["start"])
        if any(left["end"] > right["start"] for left, right in pairwise(ordered)):
            raise VPError("Source overrides overlap")
        spoken = unit["source_text"]
        for override in reversed(ordered):
            spoken = spoken[: override["start"]] + override["reading"] + spoken[override["end"] :]
        unit["spoken_text"] = spoken
    from .lexicon import validate_entry

    normalized = [validate_entry(entry) for entry in entries]
    if len({entry["sur"] for entry in normalized}) != len(normalized):
        raise VPError("Duplicate dictionary surfaces in decisions")
    unresolved = [
        candidate["id"]
        for candidate in candidates
        if candidate["id"] not in accepted
        and not any(
            override["start"] <= candidate["start"] and override["end"] >= candidate["end"]
            for override in grouped[candidate["unit_id"]]
        )
    ]
    return {
        "units": changed,
        "entries": normalized,
        "decisions": deepcopy(payload),
        "unresolved_candidates": unresolved,
    }
