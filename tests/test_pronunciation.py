from vp_manager.pronunciation import expected_reading, matched_entries


def test_matches_whole_english_terms_with_japanese_boundaries_and_longest_first():
    entries = [{"sur": "AI", "pron": "エーアイ"}, {"sur": "OpenAI", "pron": "オープンエーアイ"}]
    matches = matched_entries("OpenAIとAI、AIsは別です。", entries)
    assert [m["entry"]["sur"] for m in matches] == ["OpenAI", "AI"]
    assert (
        expected_reading("OpenAIとAI、AIsは別です。", entries) == "オープンエーアイとエーアイ、AIsは別です。"
    )


def test_reading_substitution_never_cascades():
    entries = [{"sur": "初", "pron": "次"}, {"sur": "次", "pron": "終"}]
    assert expected_reading("初と次", entries) == "次と終"
