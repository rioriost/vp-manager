import pytest

from vp_manager.common import VPError
from vp_manager.text import detect_candidates, ingest_text, plan_chunks, split_text


def test_source_units_preserve_exact_text_and_empty_paragraphs():
    source = " 一行目。\r\n\r\n二行目。\n"
    units = ingest_text(source)
    assert "".join(unit["source_text"] for unit in units) == source
    assert [unit["paragraph_index"] for unit in units] == [1, 2, 3]
    assert [chunk["text"] for chunk in plan_chunks(units)] == [" 一行目。", "二行目。"]


def test_codepoint_limit_not_utf8_or_utf16():
    source = "あ" * 139 + "😀"
    assert split_text(source) == [source]
    result = split_text(source + "い")
    assert "".join(result) == source + "い"
    assert all(len(chunk) <= 140 for chunk in result)


def test_split_keeps_latin_numeric_and_protected_names_atomic():
    source = "あ" * 132 + "HorizonDBと" + "い" * 135 + "48kHzです。"
    chunks = split_text(source)
    assert "".join(chunks) == source
    assert any("HorizonDB" in chunk for chunk in chunks)
    assert any("48kHz" in chunk for chunk in chunks)
    chunks = split_text("あ" * 136 + "東京特許許可局です。", protected=["東京特許許可局"])
    assert any("東京特許許可局" in chunk for chunk in chunks)


def test_cannot_split_one_long_token():
    with pytest.raises(VPError) as error:
        split_text("a" * 141)
    assert error.value.code == "needs_decision"


def test_split_after_reading_and_soft_breaks():
    units = ingest_text("短い原文")
    units[0]["spoken_text"] = "カ" * 141 + "\nナ\tナ"
    chunks = plan_chunks(units)
    assert all(len(chunk["text"]) <= 140 for chunk in chunks)
    assert "".join(chunk["text"] for chunk in chunks) == "カ" * 141 + "ナ ナ"
    assert units[0]["source_text"] == "短い原文"


def test_candidate_registration_is_exact_and_not_engine_unknown():
    units = ingest_text("HorizonDBとABC-Xを48kHzで比較。")
    dictionary = [
        {"sur": "HorizonDB", "pron": "ホライズンディービー"},
        {"sur": "ABC", "pron": "エービーシー"},
    ]
    candidates = detect_candidates(units, dictionary)
    by_surface = {candidate["surface"]: candidate for candidate in candidates}
    assert by_surface["HorizonDB"]["registered"] is True
    assert by_surface["HorizonDB"]["reading"] == "ホライズンディービー"
    assert by_surface["ABC-X"]["registered"] is False
    assert by_surface["ABC-X"]["kind"] == "symbol"
    assert by_surface["48kHz"]["kind"] == "number_unit"
    assert all("unknown" not in candidate for candidate in candidates)


def test_split_combining_marks_and_brackets():
    text = "あ" * 139 + "か\u3099と（東京駅）です。"
    chunks = split_text(text)
    assert "".join(chunks) == text
    assert any("か\u3099" in chunk for chunk in chunks)
    assert any("（東京駅）" in chunk for chunk in chunks)


def test_signed_numeric_candidate_cannot_be_accepted_as_unsigned():
    candidates = detect_candidates(ingest_text("値は-10と+20です。"), [])
    assert [(value["surface"], value["kind"]) for value in candidates] == [
        ("-10", "symbol"),
        ("+20", "symbol"),
    ]
