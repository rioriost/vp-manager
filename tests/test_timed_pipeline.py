"""Analyze and reading-decision integration without synthesis or real settings."""

from copy import deepcopy

import pytest
from test_pptx_video import make_video_pptx

from vp_manager import jobs, pipeline
from vp_manager.common import VPError, sha256


class SettingsOnlyEngine:
    def __init__(self, settings):
        self.settings = settings

    def __getattr__(self, name):
        pytest.fail(f"Analyze/decide unexpectedly accessed engine operation {name}")


@pytest.fixture
def environment(tmp_path):
    settings = tmp_path / "fake settings"
    settings.mkdir()
    (settings / "dic.json").write_text("[]", encoding="utf-8")
    return tmp_path, SettingsOnlyEngine(settings)


def payload(job, **changes):
    return {"schema_version": 1, "source_revision": job["source_revision"], "overrides": [], **changes}


@pytest.mark.parametrize("soft_breaks", [False, True])
def test_analyze_decide_retains_source_timeline_and_splits_only_narration(environment, soft_breaks):
    root, engine = environment
    long_body = "画面の状態を確認します。" * 16 + "XYZを選択します。"
    paragraphs = [
        "【再生前】",
        "ABCの動作を紹介します。",
        "",
        "【動画を再生】",
        "【0:30付近】",
        long_body,
        "【1:45付近】",
        "3秒待ちます。",
        "【再生後】",
        "結果を確認しました。",
    ]
    notes = ["\n".join(paragraphs)] if soft_breaks else paragraphs
    source = make_video_pptx(root / "original.pptx", notes=notes)
    original = source.read_bytes()
    dictionary_before = (engine.settings / "dic.json").read_bytes()
    directory = root / "job"
    job = pipeline.analyze(directory, engine, source=source)
    assert job["status"] == "needs_decision"
    assert [candidate["surface"] for candidate in job["candidates"]] == ["ABC", "XYZ", "3秒"]
    units_before = deepcopy(job["units"])
    slides_before = deepcopy(job["slides"])
    controls = {unit["id"] for unit in job["units"] if unit.get("note_control")}
    assert controls
    assert not controls.intersection(candidate["unit_id"] for candidate in job["candidates"])
    readings = {"ABC": "エービーシー", "XYZ": "エックスワイゼット"}
    overrides = [
        {
            "unit_id": candidate["unit_id"],
            "start": candidate["start"],
            "end": candidate["end"],
            "expected": candidate["surface"],
            "reading": readings[candidate["surface"]],
        }
        for candidate in job["candidates"]
        if candidate["surface"] in readings
    ]
    accepted = [candidate["id"] for candidate in job["candidates"] if candidate["surface"] == "3秒"]
    decisions = payload(job, overrides=overrides, accept_candidates=accepted)
    pipeline.decide(directory, job, decisions)
    # Reapplying the full decision snapshot must also keep controls out of speech.
    pipeline.decide(directory, job, decisions)
    assert job["status"] == "planned"
    assert job["unresolved_candidates"] == []
    assert job["slides"] == slides_before
    assert [unit["source_text"] for unit in job["units"]] == [unit["source_text"] for unit in units_before]
    for before, after in zip(units_before, job["units"], strict=True):
        for key in ("id", "source_unit_id", "source_start", "source_end", "paragraph_index", "note_phase"):
            assert before[key] == after[key]
        if after["note_control"]:
            assert after["spoken_text"] == ""
    assert all(0 < len(chunk["text"]) <= 140 for chunk in job["chunks"])
    assert not controls.intersection(chunk["unit_id"] for chunk in job["chunks"])
    assert all("【" not in chunk["text"] and "付近】" not in chunk["text"] for chunk in job["chunks"])
    long_unit = next(unit for unit in job["units"] if "XYZ" in unit["source_text"])
    long_chunks = [chunk for chunk in job["chunks"] if chunk["unit_id"] == long_unit["id"]]
    assert len(long_chunks) >= 2
    assert "".join(chunk["text"] for chunk in long_chunks) == long_body.replace("XYZ", readings["XYZ"])
    assert long_unit["id"] in job["slides"][0]["timeline"]["cues"][0]["unit_ids"]
    assert all(chunk["slide_id"] == "256" for chunk in job["chunks"])
    persisted = jobs.load(directory)
    assert persisted["slides"] == slides_before
    assert persisted["units"] == job["units"]
    assert persisted["chunks"] == job["chunks"]
    assert source.read_bytes() == original
    assert (directory / job["source"]["copy"]).read_bytes() == original
    assert job["source_revision"] == sha256(source)
    assert (engine.settings / "dic.json").read_bytes() == dictionary_before


def test_candidate_free_timed_notes_plan_without_speaking_markers(environment):
    root, engine = environment
    source = make_video_pptx(root / "original.pptx")
    directory = root / "job"
    job = pipeline.analyze(directory, engine, source=source)
    assert job["status"] == "planned"
    assert job["candidates"] == []
    assert [chunk["text"] for chunk in job["chunks"]] == ["説明です。", "注釈です。", "終わりです。"]
    pipeline.decide(directory, job, payload(job))
    assert [chunk["text"] for chunk in job["chunks"]] == ["説明です。", "注釈です。", "終わりです。"]
    assert job["slides"][0]["timeline"]["cues"][0]["at_seconds"] == 30


def test_control_override_rejected_without_mutating_planned_job(environment):
    root, engine = environment
    source = make_video_pptx(root / "original.pptx")
    directory = root / "job"
    job = pipeline.analyze(directory, engine, source=source)
    before = deepcopy(job)
    persisted = (directory / "job.json").read_bytes()
    control = next(unit for unit in job["units"] if unit.get("note_control"))
    with pytest.raises(VPError):
        pipeline.decide(
            directory,
            job,
            payload(
                job,
                overrides=[
                    {
                        "unit_id": control["id"],
                        "start": 0,
                        "end": len(control["source_text"]),
                        "expected": control["source_text"],
                        "reading": "サイセイマエ",
                    }
                ],
            ),
        )
    assert job == before
    assert (directory / "job.json").read_bytes() == persisted
