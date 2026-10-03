import pytest

from vp_manager.common import VPError
from vp_manager.timed_notes import parse_timed_notes


def units(*paragraphs):
    return [
        {"id": f"u{i:04d}", "slide_id": "256", "paragraph_index": i, "source_text": text}
        for i, text in enumerate(paragraphs, 1)
    ]


def test_exact_user_format_retains_text_and_assigns_timeline():
    source = units(
        "【再生前】",
        "はじめに説明します。",
        "【動画を再生】",
        "【0:30付近】",
        "画面を確認します。",
        "【1:45付近】",
        "結果を確認します。",
        "【再生後】",
        "以上です。",
    )
    parsed = parse_timed_notes(source)
    timeline = parsed["timeline"]
    assert timeline["before_unit_ids"] == ["u0002_line0001"]
    assert timeline["cues"] == [
        {"at_seconds": 30, "unit_ids": ["u0005_line0001"]},
        {"at_seconds": 105, "unit_ids": ["u0007_line0001"]},
    ]
    assert timeline["after_unit_ids"] == ["u0009_line0001"]
    assert timeline["playback_marker_unit_id"] == "u0003_line0001"
    assert [unit["source_text"] for unit in parsed["units"]] == [unit["source_text"] for unit in source]
    assert all(unit["spoken_text"] == "" for unit in parsed["units"] if unit["note_control"])
    assert "spoken_text" not in source[0]


def test_soft_breaks_crlf_blank_lines_lossless_and_source_spans():
    text = "【再生前】\r\n説明。\r\n\r\n【動画を再生】\n【0:30付近】\n注釈。\n【再生後】\nまとめ。"
    parsed = parse_timed_notes(units(text))
    assert "".join(unit["source_text"] for unit in parsed["units"]) == text
    for unit in parsed["units"]:
        assert unit["source_unit_id"] == "u0001"
        assert text[unit["source_start"] : unit["source_end"]] == unit["source_text"]
        assert unit["paragraph_index"] == 1
    assert parsed["timeline"]["cues"][0]["unit_ids"] == ["u0001_line0006"]


def test_plain_notes_do_not_activate_timing_or_change_unit_ids():
    source = units("通常の説明です。", "【注意】", "次のページ。")
    parsed = parse_timed_notes(source)
    assert parsed["timeline"] is None and parsed["units"] == source


def test_immediate_video_narration_uses_zero_offset():
    parsed = parse_timed_notes(units("【動画を再生】", "冒頭。", "【0:30付近】", "次。"))
    assert [cue["at_seconds"] for cue in parsed["timeline"]["cues"]] == [0, 30]


@pytest.mark.parametrize(
    "paragraphs",
    [
        ["【再生前】", "前だけ。"],
        ["【動画を再生】", "【動画を再生】"],
        ["前置き。", "【動画を再生】"],
        ["【再生前】", "【再生前】", "【動画を再生】"],
        ["【0:30付近】", "【動画を再生】"],
        ["【動画を再生】", "【1:45付近】", "【0:30付近】"],
        ["【動画を再生】", "【0:30付近】", "【0:30付近】"],
        ["【動画を再生】", "【0:99付近】"],
        ["【動画を再生】", "【０：３０付近】"],
        ["【再生前】説明。", "【動画を再生】"],
        ["【動画を再生】", "【再生後】", "【0:30付近】"],
        ["【動画を再生】", "【停止】"],
        ["【動画を再生】", "【再生後】", "【再生後】"],
    ],
)
def test_invalid_directives_are_decisions_not_synthesized_text(paragraphs):
    with pytest.raises(VPError) as exc:
        parse_timed_notes(units(*paragraphs))
    assert exc.value.code == "needs_decision"
