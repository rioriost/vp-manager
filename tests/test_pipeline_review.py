"""Independent pipeline qualification with local fake engine and settings only."""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf

from vp_manager import jobs, lexicon, pipeline, voicepeak
from vp_manager.common import VPError, sha256


class FakeEngine:
    def __init__(self, settings):
        self.settings = settings
        self.calls = []
        self.inventory_calls = 0
        self.assets = "assets-v1"
        self.fail_at = None

    def inventory(self):
        self.inventory_calls += 1
        return {
            "version": "1.2.23",
            "voice_asset_fingerprint": self.assets,
            "dictionary_hash": lexicon.dictionary_hash(self.settings),
            "narrators": ["Japanese Female 1"],
        }

    def validate_input(self, text, **options):
        voicepeak.validate_text(text)
        if options["narrator"] != "Japanese Female 1":
            raise VPError("Unknown narrator")

    def render(self, text, output, **options):
        self.calls.append((text, self.assets, options))
        if self.fail_at == len(self.calls):
            raise VPError("Synthetic engine failure", code="synthesis")
        output = Path(output)
        output.parent.mkdir(parents=True, exist_ok=True)
        # Different attempts intentionally produce different bytes, as a real
        # synthesis backend is not required to be byte-deterministic.
        samples = np.zeros(4800, dtype=np.float64)
        samples[480:4320] = (0.1 + len(self.calls) / 1000) * np.sin(np.arange(3840) * 0.1)
        sf.write(output, samples, 48000, subtype="PCM_16")
        return {
            "path": str(output),
            "sha256": sha256(output),
            "frames": len(samples),
            "sample_rate": 48000,
            "channels": 1,
            "duration": 0.1,
            "elapsed_seconds": 0.01,
            "dictionary_hash": lexicon.dictionary_hash(self.settings),
            "version": "1.2.23",
            "voice_asset_fingerprint": self.assets,
        }


@pytest.fixture
def environment(tmp_path, monkeypatch):
    settings = tmp_path / "settings"
    settings.mkdir()
    (settings / "dic.json").write_text("[]", encoding="utf-8")
    monkeypatch.setattr(voicepeak, "ensure_no_voicepeak", lambda: None)
    monkeypatch.setattr(voicepeak, "LOCK_PATH", tmp_path / "engine.lock")
    monkeypatch.setattr(pipeline, "STATE_DIR", tmp_path / "recovery")
    return tmp_path, FakeEngine(settings)


def start(environment, text):
    root, engine = environment
    directory = root / "job"
    job = pipeline.analyze(directory, engine, text=text)
    return directory, job, engine


def test_first_batch_reuses_identical_text_without_overwriting_earlier_hash(environment):
    directory, job, engine = start(environment, "同じ原稿です。\n同じ原稿です。")
    pipeline.render(directory, job, engine)
    assert len(engine.calls) == 1
    assert len(job["history"]) == 1
    pipeline.current_renders(directory, job)
    assert len({record["sha256"] for record in job["renders"].values()}) == 1


def test_chunk_growth_cannot_enlarge_frozen_original_unit_budget(environment):
    original = "最初です。最後です。"
    directory, job, engine = start(environment, original)
    for speed in (100, 101, 102, 103):
        pipeline.configure(directory, job, {"speed": speed})
        pipeline.render(directory, job, engine)
    assert len(engine.calls) == 4
    boundary = original.index("最後")
    pipeline.decide(
        directory,
        job,
        {
            "schema_version": 1,
            "source_revision": job["source_revision"],
            "overrides": [
                {
                    "unit_id": "u0001",
                    "start": 0,
                    "end": boundary,
                    "expected": original[:boundary],
                    "reading": "ア" * 100 + "。",
                },
                {
                    "unit_id": "u0001",
                    "start": boundary,
                    "end": len(original),
                    "expected": original[boundary:],
                    "reading": "イ" * 100 + "。",
                },
            ],
        },
    )
    assert len(job["chunks"]) == 2
    with pytest.raises(VPError) as error:
        pipeline.render(directory, job, engine)
    assert error.value.code == "needs_decision"
    assert len(engine.calls) == 4
    assert job["unit_chunk_budget"]["u0001"] == 1


def test_pending_batch_cannot_overfill_remaining_attempt_budget(environment):
    original = "あ" * 80 + "。" + "い" * 80 + "。"
    directory, job, engine = start(environment, original)
    pipeline.render(directory, job, engine)
    assert len(engine.calls) == 2
    for first, second in [(1, 0), (1, 1), (2, 1), (2, 2), (3, 2)]:
        readings = [
            "あ" * 80 + "。" if first == 0 else "カ" * (70 + first) + "。",
            "い" * 80 + "。" if second == 0 else "キ" * (70 + second) + "。",
        ]
        payload = {
            "schema_version": 1,
            "source_revision": job["source_revision"],
            "overrides": [
                {
                    "unit_id": "u0001",
                    "start": 0,
                    "end": 81,
                    "expected": original[:81],
                    "reading": readings[0],
                },
                {
                    "unit_id": "u0001",
                    "start": 81,
                    "end": 162,
                    "expected": original[81:],
                    "reading": readings[1],
                },
            ],
        }
        pipeline.decide(directory, job, payload)
        pipeline.render(directory, job, engine)
    assert len(engine.calls) == 7
    payload["overrides"][0]["reading"] = "カ" * 74 + "。"
    payload["overrides"][1]["reading"] = "キ" * 73 + "。"
    pipeline.decide(directory, job, payload)
    with pytest.raises(VPError) as error:
        pipeline.render(directory, job, engine)
    assert error.value.code == "needs_decision"
    assert len(engine.calls) <= 8
    assert len(job["history"]) <= 8


def test_failed_synthesis_is_recorded_and_unchanged_resume_never_retries(environment):
    directory, job, engine = start(environment, "検証です。")
    engine.fail_at = 1
    with pytest.raises(VPError):
        pipeline.render(directory, job, engine)
    assert jobs.load(directory)["history"][0]["status"] == "failed"
    engine.fail_at = None
    with pytest.raises(VPError) as error:
        pipeline.render(directory, jobs.load(directory), engine)
    assert error.value.code == "needs_decision"
    assert len(engine.calls) == 1


def test_global_pending_dictionary_blocks_even_cache_only_resume(environment):
    directory, job, engine = start(environment, "検証です。")
    pipeline.render(directory, job, engine)
    inventory_before = engine.inventory_calls
    journal = pipeline.STATE_DIR / "transaction-abandoned" / "journal.json"
    journal.parent.mkdir(parents=True)
    journal.write_text(json.dumps({"settings": str(engine.settings.resolve()), "status": "staged"}))
    with pytest.raises(VPError) as error:
        pipeline.render(directory, job, engine)
    assert error.value.code == "needs_recovery"
    assert len(engine.calls) == 1
    assert engine.inventory_calls == inventory_before


def test_partial_asset_upgrade_cannot_be_verified_as_one_snapshot(environment):
    directory, job, engine = start(environment, "最初です。\n次です。")
    pipeline.render(directory, job, engine)
    engine.assets = "assets-v2"
    engine.fail_at = 4
    with pytest.raises(VPError):
        pipeline.render(directory, job, engine)
    assert len(engine.calls) == 4
    with pytest.raises(VPError):
        pipeline.current_renders(directory, jobs.load(directory))


def test_approval_preserves_other_chunks_asr_and_settings_invalidate_quality(environment, monkeypatch):
    directory, job, engine = start(environment, "最初です。\n次です。")
    pipeline.render(directory, job, engine)
    expected_by_hash = {record["sha256"]: record["text"] for record in job["renders"].values()}
    monkeypatch.setattr(pipeline.qa, "transcribe", lambda path, model: expected_by_hash[sha256(path)])
    pipeline.verify(directory, job, directory / "fake-model")
    before = {key: result["content"]["transcript"] for key, result in job["qa"].items()}
    pipeline.accept(directory, job, "c0001", "listener", "Listened to first chunk")
    assert {key: result["content"]["transcript"] for key, result in job["qa"].items()} == before
    pipeline.accept(directory, job, "c0002", "listener", "Listened to second chunk")
    assert job["quality"] == "verified"
    pipeline.configure(directory, job, {"speed": 110})
    assert job["quality"] == "unreviewed"
    assert not job["qa"]
    pipeline.render(directory, job, engine)
    pipeline.verify(directory, job)
    assert job["quality"] == "draft"
    assert all(result["listening"] == "unreviewed" for result in job["qa"].values())


def test_dictionary_change_invalidates_all_cached_outputs(environment):
    directory, job, engine = start(environment, "最初です。\n次です。")
    pipeline.render(directory, job, engine)
    original_hashes = {record["sha256"] for record in job["renders"].values()}
    (engine.settings / "dic.json").write_text('[{"sur":"追加語"}]', encoding="utf-8")
    pipeline.render(directory, job, engine)
    assert len(engine.calls) == 4
    assert not original_hashes.intersection(record["sha256"] for record in job["renders"].values())
    assert {record["dictionary_hash"] for record in job["renders"].values()} == {
        lexicon.dictionary_hash(engine.settings)
    }
    pipeline.current_renders(directory, job)


def test_utf8_file_crlf_and_source_hash_are_preserved(environment):
    root, engine = environment
    source = root / "input.txt"
    original = "最初です。\r\n次です。\r\n".encode()
    source.write_bytes(original)
    directory = root / "job"
    job = pipeline.analyze(directory, engine, source=source)
    assert "".join(unit["source_text"] for unit in job["units"]).encode() == original
    assert source.read_bytes() == original
    assert (directory / job["source"]["copy"]).read_bytes() == original


def test_video_visual_uncertainty_overrides_verified_audio_quality(environment, monkeypatch):
    from vp_manager import media

    directory, job, engine = start(environment, "検証です。")
    pipeline.render(directory, job, engine)
    job["quality"] = "verified"
    monkeypatch.setattr(
        media,
        "export_video",
        lambda *args, **kwargs: {
            "video": {"path": "video/presentation.mp4"},
            "quality": "draft",
            "audio_quality": "verified",
            "visual_review": "required",
        },
    )
    pipeline.export_video(directory, job, allow_draft=True)
    assert job["quality"] == "draft"
    assert job["video_export"]["audio_quality"] == "verified"


def test_one_chunk_cannot_spend_other_chunks_retry_allowance(environment):
    original = "あ" * 80 + "。" + "い" * 80 + "。"
    directory, job, engine = start(environment, original)
    pipeline.render(directory, job, engine)
    for size in (71, 72, 73):
        payload = {
            "schema_version": 1,
            "source_revision": job["source_revision"],
            "overrides": [
                {
                    "unit_id": "u0001",
                    "start": 0,
                    "end": 81,
                    "expected": original[:81],
                    "reading": "カ" * size + "。",
                }
            ],
        }
        pipeline.decide(directory, job, payload)
        pipeline.render(directory, job, engine)
    assert len(engine.calls) == 5
    payload["overrides"][0]["reading"] = "カ" * 74 + "。"
    pipeline.decide(directory, job, payload)
    with pytest.raises(VPError):
        pipeline.render(directory, job, engine)
    assert len(engine.calls) == 5


def test_synthesis_timeout_is_limited_to_remaining_job_budget(environment, monkeypatch):
    directory, job, engine = start(environment, "検証です。")
    job["options"]["max_seconds"] = 0.1
    original = engine.render
    observed = []

    def bounded(*args, **kwargs):
        observed.append(engine.timeout)
        return original(*args, **kwargs)

    monkeypatch.setattr(engine, "render", bounded)
    pipeline.render(directory, job, engine)
    assert observed == [0.1]
    assert engine.timeout == 60
