"""Independent continuation review; all dictionaries and approvals are synthetic."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from copy import deepcopy
from pathlib import Path

import pytest

from vp_manager import jobs, lexicon, pipeline, qa, references, voicepeak
from vp_manager.common import VPError, sha256

ENTRY = {
    "sur": "ReviewAddedWord",
    "pron": "サクラモチ",
    "pos": "Japanese_Koyuumeishi_ippan",
    "accentType": 0,
}
EVIDENCE = {"test_receipt": "synthetic review fixture; never authorizes live dictionary changes"}


@pytest.fixture
def promotion_environment(tmp_path, monkeypatch):
    settings = tmp_path / "settings"
    settings.mkdir()
    original = {
        "dic.json": '[{"sur":"ExistingWord","pron":"キゾンゴ"}]\n'.encode(),
        "user.dic": b"original compiled bytes",
        "user.csv": b"original,derived,csv\n",
    }
    for name, data in original.items():
        (settings / name).write_bytes(data)
    monkeypatch.setattr(voicepeak, "ensure_no_voicepeak", lambda: None)
    monkeypatch.setattr(voicepeak, "LOCK_PATH", tmp_path / "engine.lock")
    return settings, tmp_path / "state", original


def test_promotion_replay_preserves_later_entries_and_regenerated_derivatives(promotion_environment):
    settings, state, original = promotion_environment
    baseline = lexicon.dictionary_hash(settings)
    first = lexicon.promote(settings, [ENTRY], state, expected_base_hash=baseline, evidence=EVIDENCE)
    assert first["status"] == "committed"
    assert not first["idempotent"]
    for name in ("user.dic", "user.csv"):
        assert (settings / name).read_bytes() == original[name]
    changed = lexicon.read_dictionary(settings)
    changed.append({"sur": "LaterWord", "pron": "アトノゴ"})
    (settings / "dic.json").write_text(json.dumps(changed, ensure_ascii=False), encoding="utf-8")
    (settings / "user.dic").write_bytes(b"later engine regeneration")
    later_hash = lexicon.dictionary_hash(settings)
    second = lexicon.promote(settings, [ENTRY], state, expected_base_hash=baseline, evidence=EVIDENCE)
    assert second["idempotent"]
    assert second["receipt_id"] == first["receipt_id"]
    assert second["committed_dictionary_hash"] == first["committed_dictionary_hash"]
    assert second["dictionary_hash"] == later_hash
    assert lexicon.dictionary_hash(settings) == later_hash
    assert (settings / "user.dic").read_bytes() == b"later engine regeneration"
    lexicon.assert_clean(settings, state)


@pytest.mark.parametrize(
    "new_evidence,new_entry",
    [
        ({"test_receipt": "different evidence"}, ENTRY),
        (EVIDENCE, {**ENTRY, "pron": "タケノコ"}),
    ],
)
def test_promotion_cannot_reuse_receipt_for_changed_evidence_or_entry(
    promotion_environment, new_evidence, new_entry
):
    settings, state, _ = promotion_environment
    baseline = lexicon.dictionary_hash(settings)
    lexicon.promote(settings, [ENTRY], state, expected_base_hash=baseline, evidence=EVIDENCE)
    committed = {name: (settings / name).read_bytes() for name in lexicon.FILES}
    with pytest.raises(VPError):
        lexicon.promote(settings, [new_entry], state, expected_base_hash=baseline, evidence=new_evidence)
    assert {name: (settings / name).read_bytes() for name in lexicon.FILES} == committed


@pytest.mark.parametrize("after_commit", [False, True])
def test_crash_respects_durable_promotion_commit_point(promotion_environment, after_commit):
    settings, state, original = promotion_environment
    baseline = lexicon.dictionary_hash(settings)
    script = """
import json, os, sys
from pathlib import Path
from vp_manager import lexicon, voicepeak
voicepeak.ensure_no_voicepeak = lambda: None
voicepeak.LOCK_PATH = Path(sys.argv[3])
save = lexicon._save
after_commit = sys.argv[4] == "after"
def crash_at_commit(path, record):
    if record.get("kind") == "promotion" and record.get("status") == "committed":
        if after_commit:
            save(path, record)
        os._exit(19)
    save(path, record)
lexicon._save = crash_at_commit
lexicon.promote(Path(sys.argv[1]), json.loads(sys.argv[6]), Path(sys.argv[2]),
                expected_base_hash=sys.argv[5], evidence=json.loads(sys.argv[7]))
"""
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            script,
            str(settings),
            str(state),
            str(voicepeak.LOCK_PATH),
            "after" if after_commit else "before",
            baseline,
            json.dumps([ENTRY]),
            json.dumps(EVIDENCE),
        ],
        env=environment,
        capture_output=True,
        check=False,
        timeout=20,
    )
    assert result.returncode == 19, result.stderr.decode(errors="replace")
    lexicon.recover(settings, state)
    if after_commit:
        assert any(entry["sur"] == ENTRY["sur"] for entry in lexicon.read_dictionary(settings))
        replay = lexicon.promote(settings, [ENTRY], state, expected_base_hash=baseline, evidence=EVIDENCE)
        assert replay["idempotent"]
    else:
        assert {name: (settings / name).read_bytes() for name in lexicon.FILES} == original
    lexicon.assert_clean(settings, state)


def test_promotion_rejects_changed_baseline_without_mutation(promotion_environment):
    settings, state, _ = promotion_environment
    baseline = lexicon.dictionary_hash(settings)
    changed = '[{"sur":"ExternalWord","pron":"ソトノゴ"}]'.encode()
    (settings / "dic.json").write_bytes(changed)
    with pytest.raises(VPError):
        lexicon.promote(settings, [ENTRY], state, expected_base_hash=baseline, evidence=EVIDENCE)
    assert (settings / "dic.json").read_bytes() == changed


def test_external_edit_between_install_and_commit_is_never_overwritten(promotion_environment, monkeypatch):
    settings, state, _ = promotion_environment
    baseline = lexicon.dictionary_hash(settings)
    external = '[{"sur":"OutsideWord","pron":"ソトノゴ"}]'.encode()
    save = lexicon._save

    def edit_after_install(path, record):
        save(path, record)
        if record.get("kind") == "promotion" and record.get("status") == "installed":
            (settings / "dic.json").write_bytes(external)

    monkeypatch.setattr(lexicon, "_save", edit_after_install)
    with pytest.raises(VPError) as error:
        lexicon.promote(settings, [ENTRY], state, expected_base_hash=baseline, evidence=EVIDENCE)
    assert error.value.code == "needs_recovery"
    assert (settings / "dic.json").read_bytes() == external
    with pytest.raises(VPError) as recovery:
        lexicon.recover(settings, state)
    assert recovery.value.code == "needs_recovery"
    assert (settings / "dic.json").read_bytes() == external


def test_corrupt_committed_receipt_blocks_recovery_without_rolling_back(promotion_environment):
    settings, state, _ = promotion_environment
    result = lexicon.promote(
        settings, [ENTRY], state, expected_base_hash=lexicon.dictionary_hash(settings), evidence=EVIDENCE
    )
    committed = (settings / "dic.json").read_bytes()
    journal = Path(result["receipt_path"])
    record = json.loads(journal.read_text())
    record["receipt"]["request"]["evidence"]["test_receipt"] = "corrupted evidence"
    journal.write_text(json.dumps(record), encoding="utf-8")
    with pytest.raises(VPError) as error:
        lexicon.recover(settings, state)
    assert error.value.code == "needs_recovery"
    assert (settings / "dic.json").read_bytes() == committed


def _reviewed_job(environment, monkeypatch, *, text=None, entries=None, reviewed=True):
    from test_pipeline_review import FakeEngine

    settings, state, _ = environment
    directory = settings.parent / "job"
    monkeypatch.setattr(pipeline, "STATE_DIR", state)
    engine = FakeEngine(settings)
    engine.assets = "a" * 64
    job = pipeline.analyze(directory, engine, text=text or "ReviewAddedWordを確認します。")
    pipeline.decide(
        directory,
        job,
        {
            "schema_version": 1,
            "source_revision": job["source_revision"],
            "accept_candidates": [candidate["id"] for candidate in job["candidates"]],
            "dictionary_entries": entries if entries is not None else [ENTRY],
        },
    )
    pipeline.render(directory, job, engine)
    pipeline.verify(directory, job)
    if reviewed:
        for chunk in job["chunks"]:
            pipeline.accept(directory, job, chunk["id"], "synthetic fixture", "Fake-engine test only")
    return directory, job, engine


@pytest.mark.parametrize("case", ["unheard", "unused", "substring", "stale_acceptance", "partial_review"])
def test_promotion_gate_requires_actual_used_entry_and_current_review(
    promotion_environment, monkeypatch, case
):
    settings, _, original = promotion_environment
    options = {"reviewed": case != "unheard"}
    if case == "unused":
        options["entries"] = [ENTRY, {**ENTRY, "sur": "UnusedWord"}]
    elif case == "substring":
        options.update(text="OpenAIを確認します。", entries=[{**ENTRY, "sur": "AI"}])
    elif case == "partial_review":
        options["text"] = "ReviewAddedWordを確認します。\nReviewAddedWordを再度確認します。"
    directory, job, engine = _reviewed_job(promotion_environment, monkeypatch, **options)
    if case == "unheard":
        job["quality"] = "verified"
        for report in job["qa"].values():
            report.update(status="pass", listening="accepted")
    elif case == "stale_acceptance":
        job["acceptances"]["c0001"]["evidence_key"] = "0" * 64
    elif case == "partial_review":
        job["acceptances"].pop("c0002")
    with pytest.raises(VPError) as error:
        pipeline.promote_dictionary(directory, job, engine)
    assert error.value.code in {"needs_decision", "needs_recovery"}
    assert (settings / "dic.json").read_bytes() == original["dic.json"]


def test_promotion_replays_after_commit_before_job_receipt_save(promotion_environment, monkeypatch):
    directory, job, engine = _reviewed_job(promotion_environment, monkeypatch)
    save = jobs.save

    def interrupt_receipt_save(destination, manifest):
        if manifest.get("dictionary_promotion"):
            raise RuntimeError("simulate interruption before job receipt save")
        save(destination, manifest)

    with monkeypatch.context() as context:
        context.setattr(jobs, "save", interrupt_receipt_save)
        with pytest.raises(RuntimeError):
            pipeline.promote_dictionary(directory, job, engine)
    resumed = jobs.load(directory)
    assert "dictionary_promotion_request" in resumed
    assert "dictionary_promotion" not in resumed
    assert any(entry["sur"] == ENTRY["sur"] for entry in lexicon.read_dictionary(engine.settings))
    pipeline.promote_dictionary(directory, resumed, engine)
    assert resumed["dictionary_promotion"]["idempotent"]


def test_changed_effective_reading_invalidates_same_audio_and_raw_text_approval(
    promotion_environment, monkeypatch
):
    directory, job, _ = _reviewed_job(promotion_environment, monkeypatch)
    chunk = job["chunks"][0]
    original = qa.check(
        directory / job["renders"][chunk["id"]]["path"],
        chunk["text"],
        content_expected=job["qa"][chunk["id"]]["content"]["expected"],
        provenance=job["render_snapshot"],
        acceptance=job["acceptances"][chunk["id"]],
    )
    assert original["status"] == "pass"
    result = qa.check(
        directory / job["renders"][chunk["id"]]["path"],
        chunk["text"],
        content_expected="タケノコを確認します。",
        provenance=job["render_snapshot"],
        acceptance=job["acceptances"][chunk["id"]],
    )
    assert result["status"] != "pass"
    assert result["listening"] == "unreviewed"


def test_changed_voice_assets_invalidate_same_audio_and_reading_approval(promotion_environment, monkeypatch):
    directory, job, _ = _reviewed_job(promotion_environment, monkeypatch)
    chunk = job["chunks"][0]
    result = qa.check(
        directory / job["renders"][chunk["id"]]["path"],
        chunk["text"],
        content_expected=job["qa"][chunk["id"]]["content"]["expected"],
        provenance={**job["render_snapshot"], "assets": "b" * 64},
        acceptance=job["acceptances"][chunk["id"]],
    )
    assert result["status"] != "pass"
    assert result["listening"] == "unreviewed"


def test_cache_only_resume_backfills_missing_legacy_dictionary_provenance(promotion_environment, monkeypatch):
    directory, job, engine = _reviewed_job(promotion_environment, monkeypatch)
    baseline = job.pop("render_dictionary_base_hash")
    entries = job.pop("render_dictionary_entries")
    effective = job.pop("render_lexicon")
    before = len(engine.calls)
    pipeline.render(directory, job, engine)
    assert len(engine.calls) == before
    assert job.get("render_dictionary_base_hash") == baseline
    assert job.get("render_dictionary_entries") == entries
    assert job.get("render_lexicon") == effective


def _reference_example(directory, job, *, label="accepted"):
    chunk = job["chunks"][0]
    audio = directory / job["renders"][chunk["id"]]["path"]
    copy = directory / "example.wav"
    copy.write_bytes(audio.read_bytes())
    return {
        "id": "synthetic-example",
        "audio": copy.name,
        "sha256": sha256(copy),
        "expected_text": chunk["text"],
        "provenance": references.reference_provenance(job, chunk),
        "label": label,
        "reviewer": "synthetic fixture",
        "note": "Fake-engine backend test; not an actual listening judgment",
    }


def test_reference_reuse_requires_identical_audio_reading_and_surrounding_context(
    promotion_environment, monkeypatch
):
    directory, job, _ = _reviewed_job(promotion_environment, monkeypatch)
    corpus = directory.parent / "corpus"
    before = deepcopy(job)
    references.export_reference(directory, job, corpus)
    assert job == before
    chunk = job["chunks"][0]
    audio = directory / job["renders"][chunk["id"]]["path"]
    provenance = references.reference_provenance(job, chunk)
    accepted = references.match_reference(audio, chunk["text"], provenance, corpus)
    assert accepted["status"] == "accepted"
    assert accepted["acceptance"] == job["acceptances"][chunk["id"]]
    assert accepted["naturalness_inference"] is False
    changed_context = deepcopy(job)
    changed_context["units"][0]["spoken_text"] = "別の周辺文脈です。"
    context = references.reference_provenance(changed_context, changed_context["chunks"][0])
    assert references.match_reference(audio, chunk["text"], context, corpus)["status"] == "unknown"
    changed_reading = {**provenance, "content_expected": "タケノコを確認します。"}
    assert references.match_reference(audio, chunk["text"], changed_reading, corpus)["status"] == "unknown"
    changed_audio = directory / "changed.wav"
    changed_audio.write_bytes(audio.read_bytes() + b"different bytes")
    assert references.match_reference(changed_audio, chunk["text"], provenance, corpus)["status"] == "unknown"


def test_reference_approval_reaches_pipeline_without_creating_new_listening_record(
    promotion_environment, monkeypatch
):
    directory, job, _ = _reviewed_job(promotion_environment, monkeypatch)
    corpus = directory.parent / "corpus"
    references.export_reference(directory, job, corpus)
    job["acceptances"] = {}
    job["qa"] = {}
    pipeline.verify(directory, job, corpus=corpus)
    assert job["quality"] == "verified"
    assert job["acceptances"] == {}
    assert job["reference_matches"]["c0001"]["status"] == "accepted"


def test_corrupted_reference_audio_never_reuses_stored_approval(promotion_environment, monkeypatch):
    directory, job, _ = _reviewed_job(promotion_environment, monkeypatch)
    corpus = directory.parent / "corpus"
    published = references.export_reference(directory, job, corpus)
    reference_audio = corpus / f"{published['exported'][0]}.wav"
    reference_audio.write_bytes(reference_audio.read_bytes() + b"changed")
    chunk = job["chunks"][0]
    with pytest.raises(VPError) as error:
        references.match_reference(
            directory / job["renders"][chunk["id"]]["path"],
            chunk["text"],
            references.reference_provenance(job, chunk),
            corpus,
        )
    assert error.value.code == "needs_recovery"


def test_conflicting_reference_label_cannot_replace_prior_record(promotion_environment, monkeypatch):
    directory, job, _ = _reviewed_job(promotion_environment, monkeypatch)
    corpus = directory.parent / "corpus"
    references.export_reference(directory, job, corpus)
    before = {path.name: path.read_bytes() for path in corpus.iterdir() if path.is_file()}
    example = _reference_example(directory, job, label="rejected")
    manifest = directory / "examples.json"
    manifest.write_text(json.dumps({"schema_version": 1, "examples": [example]}), encoding="utf-8")
    with pytest.raises(VPError):
        references.import_labeled_examples(manifest, corpus)
    assert {path.name: path.read_bytes() for path in corpus.iterdir() if path.is_file()} == before


def test_reference_evaluation_reports_abstention_without_naturalness_calibration_claim(
    promotion_environment, monkeypatch
):
    directory, job, _ = _reviewed_job(promotion_environment, monkeypatch)
    corpus = directory.parent / "corpus"
    references.export_reference(directory, job, corpus)
    known = _reference_example(directory, job)
    unknown = deepcopy(known)
    unknown["id"] = "synthetic-unseen-context"
    unknown["provenance"]["context"] = "b" * 64
    manifest = directory / "examples.json"
    manifest.write_text(json.dumps({"schema_version": 1, "examples": [known, unknown]}), encoding="utf-8")
    result = references.evaluate(manifest, corpus)
    assert result["total"] == 2
    assert result["correct_accepts"] == 1
    assert result["exact_in_corpus_matches"] == 1
    assert result["unknown"] == 1
    assert result["false_accepts"] == 0
    assert result["calibrated"] is False
    assert result["naturalness_inference"] is False


def test_reference_export_recovers_interruption_before_index_without_replacing_review(
    promotion_environment, monkeypatch
):
    directory, job, _ = _reviewed_job(promotion_environment, monkeypatch)
    corpus = directory.parent / "corpus"

    def interrupt_index(path, data):
        assert path.name == "index.json"
        raise RuntimeError("simulate interruption after reference metadata before index")

    with monkeypatch.context() as context:
        context.setattr(references, "atomic_json", interrupt_index)
        with pytest.raises(RuntimeError):
            references.export_reference(directory, job, corpus)
    assert not (corpus / "index.json").exists()
    orphan = next(corpus.glob("*.json"))
    original_review = orphan.read_bytes()
    references.export_reference(directory, job, corpus)
    assert orphan.read_bytes() == original_review
    assert references.index_reference(corpus)["count"] == 1


def test_cli_promotion_records_verified_fixture_receipt(promotion_environment, monkeypatch, capsys):
    from vp_manager import cli

    directory, job, engine = _reviewed_job(promotion_environment, monkeypatch)
    monkeypatch.setattr(cli, "Voicepeak", lambda *_: engine)
    assert cli.main(["promote-dictionary", str(directory), "--json"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["job_id"] == job["job_id"]
    assert result["dictionary_promotion"]["status"] == "committed"
    assert jobs.load(directory)["dictionary_promotion"] == result["dictionary_promotion"]
    assert lexicon.pending_additions(engine.settings, [ENTRY]) == []


def test_cli_prepare_slides_publishes_copy_without_changing_source(
    promotion_environment, monkeypatch, capsys
):
    from test_pipeline_review import FakeEngine
    from test_render_copy import deck

    from vp_manager import cli, pptx

    settings, _, _ = promotion_environment
    engine = FakeEngine(settings)
    source = settings.parent / "input.pptx"
    deck(source)
    original = source.read_bytes()
    directory = settings.parent / "slides-job"
    pipeline.analyze(directory, engine, source=source)
    monkeypatch.setattr(cli, "Voicepeak", lambda *_: engine)
    assert cli.main(["prepare-slides", str(directory), "--json"]) == 0
    result = json.loads(capsys.readouterr().out)
    artifact = result["artifacts"]["render_copy"]
    assert artifact["source_sha256"] == sha256(source)
    assert source.read_bytes() == original
    assert artifact["temporarily_visible"] == ["512"]
    assert all(not slide["hidden"] for slide in pptx.read_pptx(Path(artifact["render_copy"]))["slides"])
    assert jobs.load(directory)["render_copy"] == artifact
