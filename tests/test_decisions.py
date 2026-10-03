from copy import deepcopy

import pytest

from vp_manager.common import VPError
from vp_manager.decisions import apply_decisions
from vp_manager.text import detect_candidates, ingest_text


def payload(**kwargs):
    return {"schema_version": 1, "source_revision": "revision", "overrides": [], **kwargs}


def test_reading_applies_exact_span_without_modifying_source():
    units = ingest_text("ABCのABCです。")
    before = deepcopy(units)
    candidates = detect_candidates(units, [])
    data = payload(
        overrides=[{"unit_id": "u0001", "start": 4, "end": 7, "expected": "ABC", "reading": "エービーシー"}],
        accept_candidates=[candidates[0]["id"]],
    )
    result = apply_decisions(units, candidates, "revision", data)
    assert result["units"][0]["spoken_text"] == "ABCのエービーシーです。"
    assert units == before
    assert result["unresolved_candidates"] == []


@pytest.mark.parametrize(
    "change",
    [
        {"source_revision": "old"},
        {"schema_version": True},
        {"schema_version": 2},
        {"overrides": "no"},
        {"run_command": "anything"},
        {"accept_candidates": ["missing"]},
    ],
)
def test_rejects_stale_and_malformed_decision_payloads(change):
    with pytest.raises(VPError):
        apply_decisions(ingest_text("ABC"), [], "revision", payload(**change))


@pytest.mark.parametrize("reading", ["別の意味", "123", "", "<break>", "カナ\nカナ", "カナ\tカナ"])
def test_reading_rejects_semantic_rewrite_and_controls(reading):
    with pytest.raises(VPError):
        apply_decisions(
            ingest_text("ABC"),
            [],
            "revision",
            payload(
                overrides=[{"unit_id": "u0001", "start": 0, "end": 3, "expected": "ABC", "reading": reading}]
            ),
        )


def test_overlap_and_wrong_expected_rejected():
    first = {"unit_id": "u0001", "start": 0, "end": 3, "expected": "ABC", "reading": "エービーシー"}
    second = {"unit_id": "u0001", "start": 2, "end": 4, "expected": "CD", "reading": "シーディー"}
    for overrides in ([first, second], [{**first, "expected": "ABD"}], [{**first, "start": True}]):
        with pytest.raises(VPError):
            apply_decisions(ingest_text("ABCD"), [], "revision", payload(overrides=overrides))


def test_registered_symbol_requires_full_replacement():
    units = ingest_text("ABC-X")
    candidates = detect_candidates(units, [{"sur": "ABC-X", "pron": "エービーシーエックス"}])
    assert candidates[0]["registered"]
    with pytest.raises(VPError):
        apply_decisions(units, candidates, "revision", payload(accept_candidates=[candidates[0]["id"]]))
    partial = {"unit_id": "u0001", "start": 0, "end": 3, "expected": "ABC", "reading": "エービーシー"}
    result = apply_decisions(units, candidates, "revision", payload(overrides=[partial]))
    assert result["unresolved_candidates"] == [candidates[0]["id"]]
    result = apply_decisions(
        units,
        candidates,
        "revision",
        payload(overrides=[{**partial, "end": 5, "expected": "ABC-X", "reading": "エービーシーエックス"}]),
    )
    assert result["unresolved_candidates"] == []


def test_dictionary_entries_use_verified_pos_and_exact_voicepeak_fields():
    entry = {"sur": "ABC", "pron": "エービーシー", "pos": "Japanese_Koyuumeishi_ippan", "accentType": 0}
    result = apply_decisions([], [], "revision", payload(dictionary_entries=[entry]))
    assert result["entries"][0]["sur"] == "ABC"
    with pytest.raises(VPError):
        apply_decisions([], [], "revision", payload(dictionary_entries=[{**entry, "pos": "Other"}]))
