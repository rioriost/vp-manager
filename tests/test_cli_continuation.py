"""CLI reference workflow qualification; synthetic audio and local fake engine only."""

import json
from copy import deepcopy

import pytest
from test_references import examples_manifest, make_job

from vp_manager import cli, jobs, references


@pytest.fixture
def saved_job(tmp_path, monkeypatch):
    # Per-job CLI commands construct an adapter but reference/QA operations must
    # not call it. This object has no engine methods and no live settings path.
    monkeypatch.setattr(cli, "Voicepeak", lambda *args: object())
    directory = tmp_path / "job"
    job, audio = make_job(directory)
    job.update(schema_version=1, artifacts={}, issues=[])
    jobs.save(directory, job)
    return directory, job, audio


def invoke(capsys, arguments):
    code = cli.main([*map(str, arguments), "--json"])
    captured = capsys.readouterr()
    assert captured.err == ""
    result = json.loads(captured.out)
    assert result["schema_version"] == 1
    assert isinstance(result["status"], str)
    return code, result


def prohibit_engine(*args):
    raise AssertionError("Standalone corpus commands must not initialize VOICEPEAK")


def test_cli_export_list_import_evaluate_and_no_engine_for_corpus_commands(
    saved_job, tmp_path, monkeypatch, capsys
):
    directory, job, audio = saved_job
    corpus = tmp_path / "corpus"
    code, result = invoke(capsys, ["export-references", directory, "--corpus", corpus])
    assert code == 0 and result["job_id"] == job["job_id"]
    stored = jobs.load(directory)
    assert len(stored["reference_export"]["exported"]) == 1
    monkeypatch.setattr(cli, "Voicepeak", prohibit_engine)
    code, result = invoke(capsys, ["reference-list", "--corpus", corpus])
    assert code == 0 and result["status"] == "complete" and result["job_id"] is None
    assert result["count"] == 1
    assert result["entries"][0]["review"]["reviewer"] == "Fixture reviewer"
    manifest, _ = examples_manifest(tmp_path, job, audio)
    imported = tmp_path / "labeled-corpus"
    code, result = invoke(capsys, ["reference-import", "--file", manifest, "--corpus", imported])
    assert code == 0 and len(result["exported"]) == 3
    code, result = invoke(capsys, ["evaluate-references", "--file", manifest, "--corpus", imported])
    assert code == 0 and result["status"] == "complete"
    assert result["correct_accepts"] == result["correct_rejects"] == 1
    assert result["unknown"] == 1 and result["exact_in_corpus_matches"] == 3
    assert result["calibrated"] is False and result["naturalness_inference"] is False


def test_verify_exact_reference_reuses_review_without_creating_manual_acceptance(saved_job, tmp_path, capsys):
    directory, job, _ = saved_job
    corpus = tmp_path / "corpus"
    references.export_reference(directory, job, corpus)
    job["acceptances"] = {}
    jobs.save(directory, job)
    code, result = invoke(capsys, ["verify", directory, "--corpus", corpus])
    assert code == 0 and result["status"] == "checked" and result["quality"] == "verified"
    stored = jobs.load(directory)
    assert stored["acceptances"] == {}
    assert stored["reference_matches"]["c0001"]["status"] == "accepted"
    assert stored["qa"]["c0001"]["listening"] == "accepted"
    assert stored["reference_corpus"] == str(corpus.resolve())
    code, _ = invoke(capsys, ["verify", directory])
    assert code == 0  # The selected corpus remains attached to this job.


@pytest.mark.parametrize("label", ["missing", "rejected", "uncertain"])
def test_verify_unknown_and_nonaccepted_labels_remain_draft(saved_job, tmp_path, capsys, label):
    directory, job, audio = saved_job
    corpus = tmp_path / "corpus"
    if label != "missing":
        manifest, _ = examples_manifest(tmp_path, job, audio, labels=(label,))
        references.import_labeled_examples(manifest, corpus)
    job["acceptances"] = {}
    jobs.save(directory, job)
    code, result = invoke(capsys, ["verify", directory, "--corpus", corpus])
    assert code == 4 and result["status"] == "needs_revision" and result["quality"] == "draft"
    stored = jobs.load(directory)
    assert stored["qa"]["c0001"]["listening"] == "unreviewed"
    assert stored["acceptances"] == {}
    assert stored["reference_matches"]["c0001"]["status"] == (
        "rejected" if label == "rejected" else "unknown"
    )


def test_verify_corrupt_reference_returns_recovery_json_and_persists_status(saved_job, tmp_path, capsys):
    directory, job, _ = saved_job
    corpus = tmp_path / "corpus"
    exported = references.export_reference(directory, job, corpus)
    (corpus / f"{exported['exported'][0]}.wav").write_bytes(b"modified reference")
    job["acceptances"] = {}
    jobs.save(directory, job)
    code, result = invoke(capsys, ["verify", directory, "--corpus", corpus])
    assert code == 6 and result["status"] == "needs_recovery"
    assert result["issues"][0]["code"] == "needs_recovery"
    assert jobs.load(directory)["status"] == "needs_recovery"


def test_reference_export_skips_unreviewed_chunks_and_keeps_decision_exit(saved_job, tmp_path, capsys):
    directory, job, _ = saved_job
    job["status"], job["quality"] = "needs_revision", "draft"
    job["qa"]["c0001"].update(status="uncertain", listening="unreviewed")
    job["acceptances"] = {}
    jobs.save(directory, job)
    corpus = tmp_path / "corpus"
    code, result = invoke(capsys, ["export-references", directory, "--corpus", corpus])
    assert code == 4 and result["status"] == "needs_revision"
    stored = jobs.load(directory)
    assert stored["reference_export"]["exported"] == []
    assert stored["reference_export"]["skipped"][0]["chunk_id"] == "c0001"
    assert not (corpus / "index.json").exists()


def test_evaluation_false_accept_is_reported_even_when_command_completes(
    saved_job, tmp_path, monkeypatch, capsys
):
    _, job, audio = saved_job
    manifest, examples = examples_manifest(tmp_path, job, audio, labels=("accepted",))
    corpus = tmp_path / "corpus"
    references.import_labeled_examples(manifest, corpus)
    examples[0]["label"] = "rejected"
    manifest.write_text(json.dumps({"schema_version": 1, "examples": examples}))
    monkeypatch.setattr(cli, "Voicepeak", prohibit_engine)
    code, result = invoke(capsys, ["evaluate-references", "--file", manifest, "--corpus", corpus])
    assert code == 0 and result["status"] == "complete" and result["false_accepts"] == 1
    assert result["calibrated"] is False


def test_import_conflict_and_bad_json_return_structured_failures(saved_job, tmp_path, monkeypatch, capsys):
    _, job, audio = saved_job
    manifest, examples = examples_manifest(tmp_path, job, audio, labels=("accepted",))
    corpus = tmp_path / "corpus"
    references.import_labeled_examples(manifest, corpus)
    before = (corpus / "index.json").read_bytes()
    changed = deepcopy(examples)
    changed[0]["label"] = "rejected"
    manifest.write_text(json.dumps({"schema_version": 1, "examples": changed}))
    monkeypatch.setattr(cli, "Voicepeak", prohibit_engine)
    code, result = invoke(capsys, ["reference-import", "--file", manifest, "--corpus", corpus])
    assert code == 6 and result["issues"][0]["code"] == "needs_recovery"
    assert (corpus / "index.json").read_bytes() == before
    manifest.write_text(json.dumps({"schema_version": 2, "examples": []}))
    code, result = invoke(capsys, ["reference-import", "--file", manifest, "--corpus", corpus])
    assert code == 2 and result["status"] == "failed" and result["issues"][0]["code"] == "input"
