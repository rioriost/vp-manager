import json
from copy import deepcopy

import numpy as np
import pytest
import soundfile as sf

from vp_manager.common import VPError, fingerprint, sha256
from vp_manager.references import (
    MAX_JSON_BYTES,
    _evidence_key,
    evaluate,
    export_reference,
    import_labeled_examples,
    index_reference,
    match_reference,
    reference_provenance,
)


def make_job(directory):
    directory.mkdir()
    source = directory / "source.txt"
    source.write_text("カナです。", encoding="utf-8")
    audio = directory / "chunk.wav"
    samples = np.sin(np.arange(4800) / 100) * 0.1
    samples[:100] = samples[-100:] = 0
    sf.write(audio, samples, 48000, subtype="PCM_16")
    options = {"narrator": "Japanese Female 1", "speed": 100, "pitch": 0, "emotion": {}}
    snapshot = {
        "engine": "1.2.23",
        "assets": fingerprint("assets"),
        "dictionary": fingerprint("dictionary"),
        "split": 1,
        **options,
    }
    chunk = {"id": "c0001", "unit_id": "u0001", "slide_id": None, "paragraph_index": 1, "text": "カナです。"}
    job = {
        "job_id": "job1",
        "source": {"copy": "source.txt"},
        "source_revision": sha256(source),
        "status": "checked",
        "quality": "verified",
        "options": options,
        "render_snapshot": snapshot,
        "render_lexicon": [],
        "dictionary_entries": [],
        "units": [
            {
                "id": "u0001",
                "slide_id": None,
                "paragraph_index": 1,
                "source_text": "カナです。",
                "spoken_text": "カナです。",
            }
        ],
        "chunks": [chunk],
        "renders": {
            "c0001": {
                "path": "chunk.wav",
                "sha256": sha256(audio),
                "status": "complete",
                "text": chunk["text"],
                "render_key": fingerprint({**snapshot, "text": chunk["text"]}),
                "dictionary_hash": snapshot["dictionary"],
            }
        },
    }
    job["render_plan_key"] = fingerprint(
        {"source": job["source_revision"], "chunks": [chunk], "entries": [], "options": options}
    )
    provenance = reference_provenance(job, chunk)
    evidence = _evidence_key(sha256(audio), chunk["text"], provenance)
    job["qa"] = {
        "c0001": {
            "status": "pass",
            "listening": "accepted",
            "evidence_key": evidence,
            "acoustics": {"status": "pass", "sha256": sha256(audio)},
        }
    }
    job["acceptances"] = {
        "c0001": {
            "reviewer": "Fixture reviewer",
            "note": "Explicit synthetic fixture acceptance, not a real voice assessment",
            "at": "2026-10-03T00:00:00Z",
            "evidence_key": evidence,
        }
    }
    return job, audio


def test_export_only_bound_accepted_evidence_and_exact_match(tmp_path):
    directory, corpus = tmp_path / "job", tmp_path / "private-corpus"
    job, audio = make_job(directory)
    source_before = sha256(directory / "source.txt")
    job_before = deepcopy(job)
    result = export_reference(directory, job, corpus)
    assert len(result["exported"]) == 1
    assert (corpus.stat().st_mode & 0o077) == 0
    for path in corpus.iterdir():
        assert path.stat().st_mode & 0o077 == 0
    matched = match_reference(
        audio, job["chunks"][0]["text"], reference_provenance(job, job["chunks"][0]), corpus
    )
    assert matched["status"] == "accepted"
    assert matched["acceptance"] == job["acceptances"]["c0001"]
    assert matched["naturalness_inference"] is False
    assert "quality" not in matched
    assert job == job_before
    assert sha256(directory / "source.txt") == source_before
    second = export_reference(directory, job, corpus)
    assert second["exported"] == [] and second["reused"] == result["exported"]
    assert index_reference(corpus)["count"] == 1


@pytest.mark.parametrize(
    "status,listening", [("uncertain", "unreviewed"), ("fail", "accepted"), ("pass", "unreviewed")]
)
def test_unaccepted_or_draft_audio_is_not_exported(tmp_path, status, listening):
    job, _ = make_job(tmp_path / "job")
    job["qa"]["c0001"].update(status=status, listening=listening)
    job["quality"] = "draft"
    result = export_reference(tmp_path / "job", job, tmp_path / "corpus")
    assert result["exported"] == []
    assert result["skipped"][0]["reason"] == "explicit_listening_acceptance_required"
    assert not (tmp_path / "corpus").exists()


@pytest.mark.parametrize(
    "change",
    ["source", "audio", "text", "settings", "dictionary", "acceptance", "old_evidence", "intended_reading"],
)
def test_export_rejects_changed_evidence(tmp_path, change):
    directory = tmp_path / "job"
    job, audio = make_job(directory)
    if change == "source":
        (directory / "source.txt").write_text("Changed")
    elif change == "audio":
        sf.write(audio, np.zeros(4800), 48000)
    elif change == "text":
        job["chunks"][0]["text"] = "チガウ"
    elif change == "settings":
        job["options"]["speed"] = 120
    elif change == "dictionary":
        job["render_snapshot"]["dictionary"] = fingerprint("new dictionary")
    elif change == "acceptance":
        job["acceptances"]["c0001"]["evidence_key"] = "old"
    elif change == "old_evidence":
        old = fingerprint({"audio": sha256(audio), "expected": job["chunks"][0]["text"]})
        job["acceptances"]["c0001"]["evidence_key"] = old
        job["qa"]["c0001"]["evidence_key"] = old
    elif change == "intended_reading":
        job["render_lexicon"] = [{"sur": "カナ", "pron": "タケノコ"}]
    with pytest.raises(VPError):
        export_reference(directory, job, tmp_path / "corpus")
    assert not (tmp_path / "corpus/index.json").exists()


@pytest.mark.parametrize(
    "field",
    [
        "engine",
        "assets",
        "dictionary",
        "narrator",
        "speed",
        "pitch",
        "emotion",
        "context",
        "content_expected",
    ],
)
def test_same_audio_cannot_borrow_other_settings_or_context(tmp_path, field):
    job, audio = make_job(tmp_path / "job")
    corpus = tmp_path / "corpus"
    export_reference(tmp_path / "job", job, corpus)
    provenance = reference_provenance(job, job["chunks"][0])
    if field in {"assets", "dictionary", "context"}:
        provenance[field] = fingerprint("changed")
    elif field in {"speed", "pitch"}:
        provenance[field] += 1
    elif field == "emotion":
        provenance[field] = {"happy": 20}
    else:
        provenance[field] += "changed"
    assert match_reference(audio, job["chunks"][0]["text"], provenance, corpus)["status"] == "unknown"


def test_changed_audio_text_and_missing_context_abstain(tmp_path):
    job, audio = make_job(tmp_path / "job")
    corpus = tmp_path / "corpus"
    export_reference(tmp_path / "job", job, corpus)
    provenance = reference_provenance(job, job["chunks"][0])
    assert match_reference(audio, "別の原稿", provenance, corpus)["status"] == "unknown"
    missing = {key: value for key, value in provenance.items() if key != "context"}
    assert (
        match_reference(audio, "カナです。", missing, corpus)["reason"] == "incomplete_provenance_or_context"
    )
    sf.write(audio, np.zeros(100), 48000)
    assert match_reference(audio, "カナです。", provenance, corpus)["status"] == "unknown"


@pytest.mark.parametrize("tamper", ["audio", "metadata", "duplicate", "path", "oversized"])
def test_corpus_tampering_is_detected(tmp_path, tamper):
    job, audio = make_job(tmp_path / "job")
    corpus = tmp_path / "corpus"
    result = export_reference(tmp_path / "job", job, corpus)
    ident = result["exported"][0]
    index = json.loads((corpus / "index.json").read_text())
    if tamper == "audio":
        (corpus / f"{ident}.wav").write_bytes(b"changed")
    elif tamper == "metadata":
        path = corpus / f"{ident}.json"
        path.write_text(path.read_text().replace("Fixture reviewer", "Another reviewer"))
    elif tamper == "duplicate":
        index["entries"].append(index["entries"][0])
        (corpus / "index.json").write_text(json.dumps(index))
    elif tamper == "path":
        index["entries"][0]["metadata"] = "../elsewhere.json"
        (corpus / "index.json").write_text(json.dumps(index))
    else:
        (corpus / "index.json").write_bytes(b" " * (MAX_JSON_BYTES + 1))
    with pytest.raises(VPError) as failure:
        match_reference(audio, job["chunks"][0]["text"], reference_provenance(job, job["chunks"][0]), corpus)
    assert failure.value.code == "needs_recovery"


def examples_manifest(tmp_path, job, audio, labels=("accepted", "rejected", "uncertain")):
    examples = []
    for index, label in enumerate(labels):
        path = tmp_path / f"example-{index}.wav"
        data, rate = sf.read(audio)
        sf.write(path, data * (1 - index / 10), rate, subtype="PCM_16")
        examples.append(
            {
                "id": f"example{index}",
                "audio": path.name,
                "sha256": sha256(path),
                "expected_text": job["chunks"][0]["text"],
                "provenance": reference_provenance(job, job["chunks"][0]),
                "label": label,
                "reviewer": "Fixture labeler",
                "note": f"Explicit synthetic {label} label; no real listening claim",
            }
        )
    manifest = tmp_path / "examples.json"
    manifest.write_text(json.dumps({"schema_version": 1, "examples": examples}), encoding="utf-8")
    return manifest, examples


def test_labeled_evaluation_reports_exact_retrieval_and_abstention(tmp_path):
    job, audio = make_job(tmp_path / "job")
    manifest, _ = examples_manifest(tmp_path, job, audio)
    corpus = tmp_path / "corpus"
    before = evaluate(manifest, corpus)
    assert before["unknown"] == 3
    import_labeled_examples(manifest, corpus)
    report = evaluate(manifest, corpus)
    assert report["correct_accepts"] == report["correct_rejects"] == 1
    assert report["unknown"] == report["uncertain_labels"] == 1
    assert report["false_accepts"] == report["false_rejects"] == 0
    assert report["exact_in_corpus_matches"] == 3
    assert report["calibrated"] is False and report["naturalness_inference"] is False


def test_contradictory_labels_stop_and_evaluation_exposes_false_accepts(tmp_path):
    job, audio = make_job(tmp_path / "job")
    manifest, examples = examples_manifest(tmp_path, job, audio, labels=("accepted",))
    corpus = tmp_path / "corpus"
    import_labeled_examples(manifest, corpus)
    examples[0]["label"] = "rejected"
    manifest.write_text(json.dumps({"schema_version": 1, "examples": examples}))
    report = evaluate(manifest, corpus)
    assert report["false_accepts"] == 1
    with pytest.raises(VPError, match="Contradictory"):
        import_labeled_examples(manifest, corpus)
    other = {**examples[0], "id": "second", "label": "accepted"}
    manifest.write_text(json.dumps({"schema_version": 1, "examples": examples + [other]}))
    with pytest.raises(VPError, match="Contradictory"):
        evaluate(manifest, corpus)


def test_manifest_wrong_hash_duplicate_ids_and_extra_execution_fields_rejected(tmp_path):
    job, audio = make_job(tmp_path / "job")
    manifest, examples = examples_manifest(tmp_path, job, audio, labels=("accepted",))
    original = deepcopy(examples)
    for changed in [
        [{**original[0], "sha256": "0" * 64}],
        original + original,
        [{**original[0], "command": "ignored"}],
        [{**original[0], "audio": "../outside.wav"}],
    ]:
        manifest.write_text(json.dumps({"schema_version": 1, "examples": changed}))
        with pytest.raises(VPError):
            evaluate(manifest, tmp_path / "corpus")
