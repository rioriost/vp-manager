"""Japanese quote preparation is separate from immutable source and decisions."""

import hashlib
from copy import deepcopy

import pytest

from vp_manager.common import VPError
from vp_manager.text import (
    ENGINE_PREPARATION_VERSION,
    SPLIT_SCHEMA_VERSION,
    actual_spoken_text,
    ingest_text,
    plan_chunks,
    prepare_synthesis_units,
)


def digest(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def test_nested_quotes_are_audited_without_changing_source_or_user_reading():
    units = ingest_text("原稿は「変更しない」。")
    units[0]["spoken_text"] = "😀『ここで「カクニン」します。』"
    before = deepcopy(units)
    prepared = prepare_synthesis_units(units)
    unit = prepared[0]
    expected = "😀 ここで カクニン します。 "
    assert units == before
    assert unit["source_text"] == before[0]["source_text"]
    assert unit["spoken_text"] == before[0]["spoken_text"]
    assert actual_spoken_text(unit) == expected
    assert unit["engine_preparation"] == {
        "schema_version": 1,
        "replacements": [
            {"index": i, "source": char, "replacement": " "}
            for i, char in enumerate(before[0]["spoken_text"])
            if char in "「」『』"
        ],
        "input_sha256": digest(before[0]["spoken_text"]),
        "output_sha256": digest(expected),
    }
    assert unit["engine_preparation"]["replacements"][0]["index"] == 1
    assert prepare_synthesis_units(prepared) == prepared
    assert ENGINE_PREPARATION_VERSION == SPLIT_SCHEMA_VERSION == 1


def test_normalizer_changes_no_other_quotes_apostrophes_or_numeric_symbols():
    text = """"quoted" “curly” ‘single’ O'Brien don't O’Brien -10 +20 3.5 48kHz （丸）【角】[x]{y}"""
    units = ingest_text(text)
    prepared = prepare_synthesis_units(units)
    assert prepared == units
    assert actual_spoken_text(prepared[0]) == text
    assert "engine_text" not in prepared[0]
    assert "engine_preparation" not in prepared[0]


def test_changed_reading_replaces_or_removes_stale_preparation():
    old = prepare_synthesis_units(ingest_text("「元の説明」"))
    old[0]["spoken_text"] = "新しい説明"
    clean = prepare_synthesis_units(old)
    assert actual_spoken_text(clean[0]) == "新しい説明"
    assert "engine_text" not in clean[0] and "engine_preparation" not in clean[0]
    old[0]["spoken_text"] = "『別の説明』"
    newer = prepare_synthesis_units(old)
    assert actual_spoken_text(newer[0]) == " 別の説明 "
    assert newer[0]["engine_preparation"]["input_sha256"] == digest("『別の説明』")


def test_empty_user_reading_overrides_quoted_source():
    units = ingest_text("「読まない」")
    units[0]["spoken_text"] = ""
    assert prepare_synthesis_units(units) == units
    assert plan_chunks(units) == []


@pytest.mark.parametrize("text", ["「」", "『』", "「\n」", "『\r\n』", "「\t『』」"])
def test_quote_only_lines_produce_no_synthesis_chunks(text):
    units = ingest_text(text)
    assert plan_chunks(units) == []
    assert all(not actual_spoken_text(unit).strip() for unit in prepare_synthesis_units(units))
    assert "".join(unit["source_text"] for unit in units) == text


def test_soft_break_and_tab_handling_follow_preparation_without_unmatched_quotes():
    units = [
        {
            "id": "u0001",
            "slide_id": "256",
            "paragraph_index": 3,
            "source_text": "「最初。\r\n続き。\t『最後』」",
        }
    ]
    before = deepcopy(units)
    chunks = plan_chunks(units)
    assert [chunk["text"] for chunk in chunks] == [" 最初。", "続き。  最後  "]
    assert all(chunk["unit_id"] == "u0001" and chunk["slide_id"] == "256" for chunk in chunks)
    assert units == before


@pytest.mark.parametrize(
    "text", ["「" + "あ" * 139 + "」", "あ" * 139 + "『かな』", "「" + "あ" * 280 + "」"]
)
def test_preparation_precedes_the_140_codepoint_boundary(text):
    units = ingest_text(text)
    chunks = plan_chunks(units)
    assert chunks and all(0 < len(chunk["text"]) <= 140 for chunk in chunks)
    assert all(not set(chunk["text"]) & set("「」『』") for chunk in chunks)
    assert "".join(chunk["text"] for chunk in chunks).replace(" ", "") == text.translate(
        str.maketrans("", "", "「」『』")
    )
    assert units[0]["source_text"] == text


def test_protected_reading_with_quotes_stays_atomic_after_preparation():
    name = "東京特許許可局"
    text = "あ" * 135 + "「" + name + "」です。"
    chunks = plan_chunks(ingest_text(text), protected=["「" + name + "」"])
    assert any(" " + name + " " in chunk["text"] for chunk in chunks)
    assert all(len(chunk["text"]) <= 140 for chunk in chunks)
    with pytest.raises(VPError) as error:
        plan_chunks(ingest_text("「" + "a" * 141 + "」"))
    assert error.value.code == "needs_decision"


def test_controls_never_gain_preparation_or_speech_and_stale_keys_are_removed():
    units = [
        {
            "id": "u0001",
            "source_text": "【動画を再生】",
            "spoken_text": "",
            "paragraph_index": 1,
            "note_control": True,
            "engine_text": "『古い文字』",
            "engine_preparation": {"schema_version": 99},
        }
    ]
    prepared = prepare_synthesis_units(units)
    assert "engine_text" not in prepared[0] and "engine_preparation" not in prepared[0]
    assert actual_spoken_text(prepared[0]) == ""
    assert plan_chunks(prepared) == []
    assert units[0]["engine_text"] == "『古い文字』"
