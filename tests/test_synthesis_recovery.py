"""Synthetic partial-failure recovery and legacy-plan migration qualification."""

import json
from copy import deepcopy

import pytest
from test_pipeline_review import environment as environment  # noqa: PLC0414 -- pytest fixture re-export
from test_pipeline_review import start

from vp_manager import jobs, lexicon, pipeline
from vp_manager.common import VPError, fingerprint, sha256
from vp_manager.qa import evidence_key
from vp_manager.text import split_text


def with_diagnostics(engine):
    original = engine.render

    def render(text, output, **options):
        failing = engine.fail_at == len(engine.calls) + 1
        engine.last_diagnostics = {
            "returncode": -11 if failing else 0,
            "timed_out": False,
            "stderr_tail": "synthetic failure" if failing else "",
            "transport": "fixture",
        }
        return original(text, output, **options)

    engine.render = render


def fail_middle(environment):
    directory, job, engine = start(environment, "最初です。\n失敗します。\n最後です。")
    engine.fail_at = 2
    with_diagnostics(engine)
    with pytest.raises(VPError) as error:
        pipeline.render(directory, job, engine)
    assert error.value.code == "synthesis"
    return directory, job, engine


def test_middle_failure_continues_later_chunks_and_persists_exact_diagnostics(environment):
    directory, _job, engine = fail_middle(environment)
    assert [call[0] for call in engine.calls] == ["最初です。", "失敗します。", "最後です。"]
    saved = jobs.load(directory)
    assert [item["status"] for item in saved["history"]] == ["complete", "failed", "complete"]
    assert set(saved["renders"]) == {"c0001", "c0003"}
    assert saved["render_progress"] == {"total": 3, "complete": 2, "blocked": 1, "pending": 0}
    assert saved["failed_chunks"][0]["chunk_id"] == "c0002"
    diagnostics = saved["history"][1]["diagnostics"]
    assert diagnostics == {
        "returncode": -11,
        "timed_out": False,
        "stderr_tail": "synthetic failure",
        "transport": "fixture",
    }
    assert saved["failed_chunks"][0]["diagnostics"] == diagnostics
    assert "render_plan_key" not in saved
    assert (engine.settings / "dic.json").read_text() == "[]"


def test_duplicate_failed_text_is_invoked_once_and_resume_never_retries_it(environment):
    directory, job, engine = start(environment, "最初です。\n失敗します。\n失敗します。\n最後です。")
    engine.fail_at = 2
    with pytest.raises(VPError):
        pipeline.render(directory, job, engine)
    assert [call[0] for call in engine.calls] == ["最初です。", "失敗します。", "最後です。"]
    assert [item["chunk_id"] for item in job["failed_chunks"]] == ["c0002", "c0003"]
    before = deepcopy(job["history"])
    completed = {
        key: (item["path"], item["sha256"], (directory / item["path"]).read_bytes())
        for key, item in job["renders"].items()
    }
    engine.fail_at = None
    resumed = jobs.load(directory)
    with pytest.raises(VPError) as error:
        pipeline.render(directory, resumed, engine)
    assert error.value.code == "needs_decision"
    assert len(engine.calls) == 3
    assert resumed["history"] == before
    assert resumed["render_progress"] == {"total": 4, "complete": 2, "blocked": 2, "pending": 0}
    assert all(item["reason"] == "unchanged_failure" for item in resumed["failed_chunks"])
    for key, (path, checksum, data) in completed.items():
        assert resumed["renders"][key]["sha256"] == checksum
        assert (directory / path).read_bytes() == data


def test_changed_reading_recovers_only_failed_chunk_with_success_cache_preserved(environment):
    directory, job, engine = fail_middle(environment)
    previous_history = deepcopy(job["history"])
    previous_complete = {key: deepcopy(value) for key, value in job["renders"].items()}
    source_before = (directory / job["source"]["copy"]).read_bytes()
    target = job["units"][1]
    pipeline.decide(
        directory,
        job,
        {
            "schema_version": 1,
            "source_revision": job["source_revision"],
            "overrides": [
                {
                    "unit_id": target["id"],
                    "start": 0,
                    "end": len("失敗します。"),
                    "expected": "失敗します。",
                    "reading": "カイフクシマス。",
                }
            ],
        },
    )
    engine.fail_at = None
    pipeline.render(directory, job, engine)
    assert len(engine.calls) == 4 and engine.calls[-1][0] == "カイフクシマス。"
    assert job["history"][:3] == previous_history
    assert job["failed_chunks"] == []
    assert job["render_progress"] == {"total": 3, "complete": 3, "blocked": 0, "pending": 0}
    for key, record in previous_complete.items():
        assert job["renders"][key]["sha256"] == record["sha256"]
        assert sha256(directory / record["path"]) == record["sha256"]
    assert (directory / job["source"]["copy"]).read_bytes() == source_before
    pipeline.current_renders(directory, job)


def seed_legacy_plan(directory, job, engine, *, first_failed):
    """Create a pre-normalizer history using the unchanged lossless splitter."""
    legacy = []
    for unit in job["units"]:
        unit.pop("engine_text", None)
        unit.pop("engine_preparation", None)
        for text in split_text(unit["source_text"].rstrip("\r\n")):
            legacy.append(
                {
                    "id": f"c{len(legacy) + 1:04d}",
                    "unit_id": unit["id"],
                    "text": text,
                    "slide_id": unit["slide_id"],
                    "paragraph_index": unit["paragraph_index"],
                }
            )
    job["chunks"] = legacy
    snapshot = {
        "engine": "1.2.23",
        "assets": engine.assets,
        "dictionary": lexicon.dictionary_hash(engine.settings),
        "split": 1,
        **pipeline.engine_options(job),
    }
    history = []
    for index, chunk in enumerate(legacy):
        key = fingerprint({**snapshot, "text": chunk["text"]})
        record = {
            "chunk_id": chunk["id"],
            "unit_id": chunk["unit_id"],
            "text": chunk["text"],
            "render_key": key,
            "path": f"audio/{key}.wav",
            "dictionary_hash": snapshot["dictionary"],
            "render_snapshot": deepcopy(snapshot),
            "elapsed_seconds": 0.01,
        }
        if first_failed and chunk["unit_id"] == legacy[0]["unit_id"]:
            record.update(
                status="failed",
                error="legacy synthetic failure",
                error_code="synthesis",
                diagnostics={"returncode": -11},
            )
        else:
            metrics = engine.render(chunk["text"], directory / record["path"], **pipeline.engine_options(job))
            record.update({k: value for k, value in metrics.items() if k != "path"})
            record["status"] = "complete"
            job["renders"][chunk["id"]] = deepcopy(record)
        history.append(record)
    job["history"] = history
    job["render_snapshot"] = snapshot
    if not first_failed:
        job["render_plan_key"] = pipeline._plan_key(job)
    jobs.save(directory, job)
    return history


def test_old_140_quote_split_failure_replans_without_erasing_source_history_or_budget(environment):
    text = "「" + "あ" * 139 + "」\n変更しない区間。"
    directory, job, engine = start(environment, text)
    seed_legacy_plan(directory, job, engine, first_failed=True)
    assert [len(chunk["text"]) for chunk in job["chunks"][:2]] == [140, 1]
    original_history = deepcopy(job["history"])
    cached = deepcopy(job["history"][-1])
    old_audio = (directory / cached["path"]).read_bytes()
    source = (directory / job["source"]["copy"]).read_bytes()
    decisions = deepcopy(job.get("decisions", {}))
    calls_before = len(engine.calls)
    pipeline.render(directory, job, engine)
    assert len(engine.calls) == calls_before + 1
    assert all(not set(call[0]) & set("「」『』") for call in engine.calls[calls_before:])
    assert job["history"][: len(original_history)] == original_history
    assert job["unit_chunk_budget"]["u0001"] == 2
    assert job["options"]["max_attempts"] == 4
    assert job.get("decisions", {}) == decisions
    assert (directory / job["source"]["copy"]).read_bytes() == source
    assert "".join(unit["source_text"] for unit in job["units"]) == text
    assert len(job["chunks"]) == 2
    assert job["renders"]["c0002"]["sha256"] == cached["sha256"]
    assert (directory / cached["path"]).read_bytes() == old_audio
    assert job["plan_updates"][-1]["reason"] == "engine_text_preparation"
    pipeline.current_renders(directory, job)


def test_legacy_acceptance_does_not_accept_new_prepared_audio(environment):
    directory, job, engine = start(environment, "「確認します。」")
    seed_legacy_plan(directory, job, engine, first_failed=False)
    old = job["history"][0]
    old_key = evidence_key(
        old["sha256"], old["text"], content_expected=old["text"], provenance=job["render_snapshot"]
    )
    job["acceptances"]["c0001"] = {
        "evidence_key": old_key,
        "reviewer": "synthetic fixture",
        "note": "fixture label; not a real listening result",
        "at": "fixture",
    }
    job["quality"] = "verified"
    job["qa"] = {"c0001": {"status": "pass", "evidence_key": old_key}}
    job["artifacts"] = {"audio": {"path": "old.wav"}}
    pipeline.render(directory, job, engine)
    assert job["renders"]["c0001"]["sha256"] != old["sha256"]
    assert job["quality"] == "unreviewed" and job["qa"] == {} and job["artifacts"] == {}
    pipeline.verify(directory, job)
    assert job["qa"]["c0001"]["evidence_key"] != old_key
    assert job["qa"]["c0001"]["listening"] != "accepted"
    assert job["quality"] != "verified"


@pytest.mark.parametrize("stage", ["verify", "assemble", "export_video", "export_references", "accept"])
def test_partial_batch_cannot_enter_complete_output_stages(environment, stage):
    directory, job, engine = fail_middle(environment)
    operation = getattr(pipeline, stage)
    kwargs = {"allow_draft": True} if stage in {"assemble", "export_video"} else {}
    args = [directory, job]
    if stage == "export_references":
        args.append(directory / "corpus")
    if stage == "accept":
        args.extend(["c0001", "fixture", "fixture"])
    with pytest.raises(VPError) as error:
        operation(*args, **kwargs)
    assert error.value.code == "needs_decision"
    assert job["artifacts"] == {} and job["qa"] == {}
    assert len(engine.calls) == 3


def test_synthesis_failure_followed_by_environment_failure_stops_and_saves_pending(environment):
    directory, job, engine = start(environment, "最初です。\n次です。\n未実行です。")
    original = engine.render
    engine.fail_at = 1

    def render(text, output, **options):
        if len(engine.calls) == 1:
            engine.calls.append((text, engine.assets, options))
            engine.last_diagnostics = {"stage": "environment", "reason": "fixture GUI appeared"}
            raise VPError("Synthetic environment failure", code="environment")
        engine.last_diagnostics = {"stage": "synthesis", "returncode": -11}
        return original(text, output, **options)

    engine.render = render
    with pytest.raises(VPError) as error:
        pipeline.render(directory, job, engine)
    assert error.value.code == "environment" and len(engine.calls) == 2
    saved = jobs.load(directory)
    assert saved["render_progress"] == {"total": 3, "complete": 0, "blocked": 2, "pending": 1}
    assert [item["chunk_id"] for item in saved["failed_chunks"]] == ["c0001", "c0002"]
    assert saved["failed_chunks"][0]["diagnostics"]["returncode"] == -11
    assert saved["failed_chunks"][1]["diagnostics"]["stage"] == "environment"
    assert "render_plan_key" not in saved


def test_dictionary_change_after_failed_invocation_prevents_later_synthesis(environment):
    directory, job, engine = start(environment, "最初です。\n次です。")
    changed = [{"sur": "外部変更", "pron": "ガイブヘンコウ"}]

    def render(text, output, **options):
        engine.calls.append((text, engine.assets, options))
        (engine.settings / "dic.json").write_text(json.dumps(changed, ensure_ascii=False))
        raise VPError("Synthetic failed invocation", code="synthesis")

    engine.render = render
    with pytest.raises(VPError) as error:
        pipeline.render(directory, job, engine)
    assert error.value.code == "needs_recovery"
    assert len(engine.calls) == 1
    assert jobs.load(directory)["history"][0]["status"] == "failed"
    assert json.loads((engine.settings / "dic.json").read_text()) == changed


def test_two_chunk_partial_batch_reports_one_complete_and_one_blocked(environment):
    directory, job, engine = start(environment, "失敗します。\n続行します。")
    engine.fail_at = 1
    with pytest.raises(VPError):
        pipeline.render(directory, job, engine)
    assert len(engine.calls) == 2
    assert jobs.load(directory)["render_progress"] == {
        "total": 2,
        "complete": 1,
        "blocked": 1,
        "pending": 0,
    }


def test_prior_completed_cache_is_counted_when_next_invocation_has_fatal_environment(environment):
    directory, job, engine = start(environment, "最初です。\n次です。\n未実行です。")
    seed_legacy_plan(directory, job, engine, first_failed=False)
    # A local fixture representing a batch saved after only its first success.
    first = deepcopy(job["history"][0])
    job["history"] = [first]
    job["renders"] = {"c0001": deepcopy(first)}
    job.pop("render_plan_key", None)
    calls_before = len(engine.calls)

    def render(text, output, **options):
        engine.calls.append((text, engine.assets, options))
        raise VPError("Synthetic environment failure", code="environment")

    engine.render = render
    with pytest.raises(VPError) as error:
        pipeline.render(directory, job, engine)
    assert error.value.code == "environment"
    assert len(engine.calls) == calls_before + 1
    saved = jobs.load(directory)
    assert saved["render_progress"] == {"total": 3, "complete": 1, "blocked": 1, "pending": 1}
    assert saved["renders"]["c0001"]["sha256"] == first["sha256"]
    assert sha256(directory / first["path"]) == first["sha256"]
    assert saved["history"][0] == first
