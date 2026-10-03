"""Locally trusted listening records: exact evidence reuse, never a prosody classifier."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path

from .common import VPError, atomic_json, file_lock, fingerprint, sha256

SCHEMA = 1
MAX_JSON_BYTES = 2 * 1024 * 1024
MAX_AUDIO_BYTES = 256 * 1024 * 1024
MAX_ENTRIES = 10000
TRUST_MODEL = "local_review_records_not_signed"
METHOD = "exact_bytes_text_provenance_context"
_HASH = re.compile(r"[0-9a-f]{64}\Z")
_RENDER_FIELDS = {"engine", "assets", "dictionary", "split", "narrator", "speed", "pitch", "emotion"}
_PROVENANCE_FIELDS = _RENDER_FIELDS | {"context", "content_expected"}


def _hash(value) -> bool:
    return isinstance(value, str) and bool(_HASH.fullmatch(value))


def _read_json(path: Path) -> dict:
    try:
        if path.is_symlink() or path.stat().st_size > MAX_JSON_BYTES:
            raise VPError("Reference JSON is a symlink or exceeds the size bound", code="needs_recovery")

        def unique(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise VPError("Duplicate JSON object key in reference data", code="needs_recovery")
                result[key] = value
            return result

        value = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=unique)
    except (OSError, ValueError) as exc:
        raise VPError(f"Cannot read reference data: {exc}", code="needs_recovery") from exc
    if not isinstance(value, dict):
        raise VPError("Reference JSON must be an object", code="needs_recovery")
    return value


def _basename(directory: Path, name: str) -> Path:
    if (
        not isinstance(name, str)
        or name in {"", ".", ".."}
        or Path(name).name != name
        or "/" in name
        or "\\" in name
    ):
        raise VPError("Corpus artifact paths must be plain basenames", code="needs_recovery")
    path = directory / name
    if path.is_symlink():
        raise VPError("Corpus artifacts must not be symlinks", code="needs_recovery")
    return path


def _audio_hash(path: Path) -> str:
    try:
        if not path.is_file() or path.is_symlink() or not 0 < path.stat().st_size <= MAX_AUDIO_BYTES:
            raise VPError(
                "Reference audio is missing, linked or exceeds the size bound", code="needs_recovery"
            )
        return sha256(path)
    except OSError as exc:
        raise VPError(f"Cannot read reference audio: {exc}", code="needs_recovery") from exc


def _valid_provenance(value) -> bool:
    if not isinstance(value, dict) or set(value) != _PROVENANCE_FIELDS:
        return False
    if not all(_hash(value[key]) for key in ("assets", "dictionary", "context")):
        return False
    if not all(isinstance(value[key], str) and value[key].strip() for key in ("engine", "narrator")):
        return False
    if not isinstance(value["content_expected"], str) or not 0 < len(value["content_expected"]) <= 16384:
        return False
    if type(value["split"]) is not int or value["split"] < 1:
        return False
    if type(value["speed"]) is not int or type(value["pitch"]) is not int:
        return False
    return isinstance(value["emotion"], dict) and all(
        isinstance(key, str) and type(level) is int and 0 <= level <= 100
        for key, level in value["emotion"].items()
    )


def reference_provenance(job: dict, chunk: dict) -> dict:
    """Bind a reference to the source and all neighboring reading decisions."""
    if not _hash(job.get("source_revision")):
        raise VPError("Reference provenance requires a source SHA-256")
    chunks = job.get("chunks", [])
    positions = [index for index, current in enumerate(chunks) if current.get("id") == chunk.get("id")]
    if len(positions) != 1 or chunks[positions[0]] != chunk:
        raise VPError("Reference chunk is not uniquely present in the current plan")
    context = {
        "source_revision": job["source_revision"],
        "units": [
            {key: unit.get(key) for key in ("id", "slide_id", "paragraph_index", "source_text")}
            | {"spoken_text": unit.get("spoken_text", unit["source_text"])}
            for unit in job.get("units", [])
        ],
        "chunks": [
            {key: current.get(key) for key in ("unit_id", "slide_id", "paragraph_index", "text")}
            for current in chunks
        ],
        "target_position": positions[0],
    }
    provenance = deepcopy(job.get("render_snapshot", {}))
    provenance["context"] = fingerprint(context)
    from .pronunciation import expected_reading

    provenance["content_expected"] = expected_reading(chunk["text"], job.get("render_lexicon", []))
    if not _valid_provenance(provenance):
        raise VPError("Incomplete engine, voice asset, dictionary or context provenance")
    return provenance


def _identity(audio_hash: str, expected: str, provenance: dict) -> dict:
    return {"audio_sha256": audio_hash, "expected_text": expected, "provenance": provenance}


def _evidence_key(audio_hash: str, expected: str, provenance: dict) -> str:
    from .qa import evidence_key

    return evidence_key(
        audio_hash,
        expected,
        content_expected=provenance["content_expected"],
        provenance={key: provenance[key] for key in _RENDER_FIELDS},
    )


def _validate_entry(entry: dict) -> None:
    fields = {
        "schema_version",
        "id",
        "audio",
        "audio_sha256",
        "expected_text",
        "provenance",
        "label",
        "review",
        "source",
        "created_at",
        "trust_model",
    }
    if set(entry) != fields or type(entry["schema_version"]) is not int or entry["schema_version"] != SCHEMA:
        raise VPError("Invalid reference entry schema", code="needs_recovery")
    if not _hash(entry["audio_sha256"]) or not _valid_provenance(entry["provenance"]):
        raise VPError("Invalid reference identity or provenance", code="needs_recovery")
    if not isinstance(entry["expected_text"], str) or not 0 < len(entry["expected_text"]) <= 140:
        raise VPError("Reference expected text must contain 1–140 code points", code="needs_recovery")
    ident = fingerprint(_identity(entry["audio_sha256"], entry["expected_text"], entry["provenance"]))
    if (
        entry["id"] != ident
        or entry["audio"] != f"{ident}.wav"
        or entry["label"] not in {"accepted", "rejected", "uncertain"}
    ):
        raise VPError("Reference ID, label or audio name disagrees with its evidence", code="needs_recovery")
    review = entry["review"]
    if not isinstance(review, dict) or set(review) != {"reviewer", "note", "evidence_key", "at"}:
        raise VPError("Reference needs explicit listening evidence", code="needs_recovery")
    if any(not isinstance(review[key], str) or not review[key].strip() for key in ("reviewer", "note", "at")):
        raise VPError("Listening reviewer, note and timestamp must be present", code="needs_recovery")
    if review["evidence_key"] != _evidence_key(
        entry["audio_sha256"], entry["expected_text"], entry["provenance"]
    ):
        raise VPError("Reference listening evidence is stale", code="needs_recovery")
    if (
        entry["trust_model"] != TRUST_MODEL
        or not isinstance(entry["source"], dict)
        or not isinstance(entry["created_at"], str)
    ):
        raise VPError("Invalid reference audit metadata", code="needs_recovery")
    if len(json.dumps(entry, ensure_ascii=False, indent=2).encode()) + 1 > MAX_JSON_BYTES:
        raise VPError("Reference metadata exceeds the size bound")


def _load(directory: Path) -> tuple[dict, list[dict]]:
    path = directory / "index.json"
    if path.is_symlink():
        raise VPError("Corpus index must not be a symlink", code="needs_recovery")
    if not path.exists():
        return {"schema_version": SCHEMA, "entries": [], "trust_model": TRUST_MODEL}, []
    index = _read_json(path)
    if (
        set(index) != {"schema_version", "entries", "trust_model"}
        or type(index["schema_version"]) is not int
        or index["schema_version"] != SCHEMA
        or index["trust_model"] != TRUST_MODEL
    ):
        raise VPError("Invalid corpus index schema", code="needs_recovery")
    if not isinstance(index["entries"], list) or len(index["entries"]) > MAX_ENTRIES:
        raise VPError("Invalid or oversized corpus index", code="needs_recovery")
    entries, seen = [], set()
    for record in index["entries"]:
        if (
            not isinstance(record, dict)
            or set(record) != {"id", "metadata", "sha256"}
            or not _hash(record["id"])
            or not _hash(record["sha256"])
        ):
            raise VPError("Invalid reference index record", code="needs_recovery")
        if record["id"] in seen or record["metadata"] != f"{record['id']}.json":
            raise VPError("Duplicate or conflicting reference ID", code="needs_recovery")
        seen.add(record["id"])
        metadata = _basename(directory, record["metadata"])
        entry = _read_json(metadata)
        if sha256(metadata) != record["sha256"]:
            raise VPError("Reference metadata hash changed", code="needs_recovery")
        _validate_entry(entry)
        if (
            entry["id"] != record["id"]
            or _audio_hash(_basename(directory, entry["audio"])) != entry["audio_sha256"]
        ):
            raise VPError("Reference audio or identity changed", code="needs_recovery")
        entries.append(entry)
    return index, entries


def _immutable(path: Path, data: bytes) -> None:
    if path.exists():
        if path.is_symlink() or path.read_bytes() != data:
            raise VPError("Existing immutable corpus file conflicts with new evidence", code="needs_recovery")
        return
    fd, temporary = tempfile.mkstemp(prefix=".reference-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, path)  # Publish without overwriting an immutable file.
    finally:
        Path(temporary).unlink(missing_ok=True)


def _publish(directory: Path, proposals: list[tuple[dict, Path]]) -> dict:
    directory = directory.expanduser().resolve()
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(directory, 0o700)
    with file_lock(directory / ".references.lock"):
        index, entries = _load(directory)
        existing = {entry["id"]: entry for entry in entries}
        pending, reused = {}, []
        for entry, audio in proposals:
            _validate_entry(entry)
            ident = entry["id"]
            other = existing.get(ident) or (pending[ident][0] if ident in pending else None)
            if other:
                if other["label"] != entry["label"]:
                    raise VPError(
                        "Contradictory listening labels for identical evidence", code="needs_recovery"
                    )
                if ident in existing:
                    reused.append(ident)
                continue
            orphan_path = _basename(directory, f"{ident}.json")
            if orphan_path.exists():
                # A previous process may have died after immutable publication
                # but before the index commit. Preserve the original review.
                orphan = _read_json(orphan_path)
                _validate_entry(orphan)
                if orphan["id"] != ident or orphan["label"] != entry["label"]:
                    raise VPError("Conflicting orphan reference evidence", code="needs_recovery")
                entry = orphan
            pending[ident] = (entry, audio)
        if len(existing) + len(pending) > MAX_ENTRIES:
            raise VPError("Reference corpus entry limit exceeded")
        prospective = {
            **index,
            "entries": index["entries"]
            + [{"id": ident, "metadata": f"{ident}.json", "sha256": "0" * 64} for ident in pending],
        }
        if len(json.dumps(prospective, ensure_ascii=False, indent=2).encode()) + 1 > MAX_JSON_BYTES:
            raise VPError("Reference index size limit exceeded")
        for ident, (entry, audio) in pending.items():
            if _audio_hash(audio) != entry["audio_sha256"]:
                raise VPError("Audio changed before reference publication", code="needs_recovery")
            data = audio.read_bytes()
            if hashlib.sha256(data).hexdigest() != entry["audio_sha256"]:
                raise VPError("Audio changed while copying reference", code="needs_recovery")
            _immutable(directory / entry["audio"], data)
            metadata = directory / f"{ident}.json"
            _immutable(
                metadata,
                (
                    json.dumps(entry, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n"
                ).encode(),
            )
            index["entries"].append({"id": ident, "metadata": metadata.name, "sha256": sha256(metadata)})
        if pending or not (directory / "index.json").exists():
            atomic_json(directory / "index.json", index)
        _load(directory)
    return {
        "exported": list(pending),
        "reused": sorted(set(reused)),
        "index_path": str(directory / "index.json"),
        "trust_model": TRUST_MODEL,
    }


def _job_path(directory: Path, name: str) -> Path:
    if not isinstance(name, str) or Path(name).is_absolute():
        raise VPError("Job artifact path must be relative")
    path = (directory / name).resolve()
    if not path.is_relative_to(directory.resolve()):
        raise VPError("Job artifact path escapes its directory")
    return path


def export_reference(job_dir: Path, job: dict, destination: Path) -> dict:
    """Copy only current, explicitly listened-to accepted chunks into the corpus."""
    directory = Path(job_dir).resolve()
    source = _job_path(directory, job.get("source", {}).get("copy"))
    if not source.is_file() or sha256(source) != job.get("source_revision"):
        raise VPError("Immutable source changed before reference export", code="needs_recovery")
    if job.get("status") in {"needs_recovery", "rendering", "failed", "cancelled"}:
        raise VPError("Unfinished or failed jobs cannot export references", code="needs_decision")
    snapshot = job.get("render_snapshot", {})
    if any(
        job.get("options", {}).get(key) != snapshot.get(key)
        for key in ("narrator", "speed", "pitch", "emotion")
    ):
        raise VPError("Current voice settings differ from the rendered snapshot", code="needs_decision")
    plan = {
        "source": job["source_revision"],
        "chunks": [
            {key: chunk[key] for key in ("id", "unit_id", "text", "slide_id", "paragraph_index")}
            for chunk in job.get("chunks", [])
        ],
        "entries": job.get("dictionary_entries", []),
        "options": {
            key: job.get("options", {}).get(key) for key in ("narrator", "speed", "pitch", "emotion")
        },
    }
    if job.get("render_plan_key") != fingerprint(plan):
        raise VPError("Reference export requires the current completed rendering plan", code="needs_decision")
    proposals, skipped = [], []
    for chunk in job.get("chunks", []):
        qa = job.get("qa", {}).get(chunk["id"], {})
        if (
            qa.get("status") != "pass"
            or qa.get("listening") != "accepted"
            or qa.get("acoustics", {}).get("status") != "pass"
        ):
            skipped.append({"chunk_id": chunk["id"], "reason": "explicit_listening_acceptance_required"})
            continue
        record = job.get("renders", {}).get(chunk["id"], {})
        if (
            record.get("status") != "complete"
            or record.get("text") != chunk["text"]
            or record.get("render_key") != fingerprint({**snapshot, "text": chunk["text"]})
            or record.get("dictionary_hash") != snapshot.get("dictionary")
        ):
            raise VPError("Stale rendering provenance cannot become a reference", code="needs_recovery")
        audio = _job_path(directory, record.get("path"))
        audio_hash = _audio_hash(audio)
        provenance = reference_provenance(job, chunk)
        evidence_key = _evidence_key(audio_hash, chunk["text"], provenance)
        acceptance = job.get("acceptances", {}).get(chunk["id"], {})
        if (
            record.get("sha256") != audio_hash
            or qa.get("acoustics", {}).get("sha256") != audio_hash
            or qa.get("evidence_key") != evidence_key
            or acceptance.get("evidence_key") != evidence_key
        ):
            raise VPError(
                "Listening acceptance is not bound to the current audio and reading", code="needs_recovery"
            )
        ident = fingerprint(_identity(audio_hash, chunk["text"], provenance))
        review = {key: acceptance.get(key) for key in ("reviewer", "note", "evidence_key", "at")}
        entry = {
            "schema_version": SCHEMA,
            "id": ident,
            "audio": f"{ident}.wav",
            "audio_sha256": audio_hash,
            "expected_text": chunk["text"],
            "provenance": provenance,
            "label": "accepted",
            "review": review,
            "source": {
                "job_id": job.get("job_id"),
                "source_revision": job["source_revision"],
                "chunk_id": chunk["id"],
            },
            "created_at": datetime.now(UTC).isoformat(),
            "trust_model": TRUST_MODEL,
        }
        proposals.append((entry, audio))
    if sha256(source) != job["source_revision"]:
        raise VPError("Source changed while exporting references", code="needs_recovery")
    if not proposals:
        return {
            "exported": [],
            "reused": [],
            "skipped": skipped,
            "index_path": str(Path(destination).resolve() / "index.json"),
            "trust_model": TRUST_MODEL,
        }
    result = _publish(Path(destination), proposals)
    result["skipped"] = skipped
    return result


def match_reference(audio_path: Path, expected_text: str, render_provenance: dict, corpus_dir: Path) -> dict:
    """Abstain unless all bytes, reading, production settings and context are identical."""
    if not isinstance(expected_text, str) or not _valid_provenance(render_provenance):
        return {
            "status": "unknown",
            "method": METHOD,
            "trust_model": TRUST_MODEL,
            "naturalness_inference": False,
            "reason": "incomplete_provenance_or_context",
        }
    _, entries = _load(Path(corpus_dir).expanduser().resolve())
    return _match_loaded(
        _audio_hash(Path(audio_path)),
        expected_text,
        render_provenance,
        {entry["id"]: entry for entry in entries},
    )


def _match_loaded(audio_hash: str, expected_text: str, render_provenance: dict, entries: dict) -> dict:
    unknown = {
        "status": "unknown",
        "method": METHOD,
        "trust_model": TRUST_MODEL,
        "naturalness_inference": False,
    }
    ident = fingerprint(_identity(audio_hash, expected_text, render_provenance))
    entry = entries.get(ident)
    if entry:
        return {
            **unknown,
            "status": entry["label"] if entry["label"] != "uncertain" else "unknown",
            "reference_id": ident,
            "evidence_key": entry["review"]["evidence_key"],
            "acceptance": deepcopy(entry["review"]) if entry["label"] == "accepted" else None,
            "review": deepcopy(entry["review"]),
            "reason": "exact_in_corpus_match",
            "label": entry["label"],
        }
    return {**unknown, "reason": "no_exact_reference"}


def index_reference(corpus_dir: Path) -> dict:
    _, entries = _load(Path(corpus_dir).expanduser().resolve())
    return {
        "schema_version": SCHEMA,
        "trust_model": TRUST_MODEL,
        "count": len(entries),
        "entries": [
            {key: deepcopy(entry[key]) for key in ("id", "expected_text", "provenance", "label", "review")}
            for entry in entries
        ],
    }


def _examples(manifest_path: Path) -> list[tuple[dict, Path]]:
    manifest_path = Path(manifest_path).expanduser().resolve()
    manifest = _read_json(manifest_path)
    if (
        set(manifest) != {"schema_version", "examples"}
        or type(manifest["schema_version"]) is not int
        or manifest["schema_version"] != SCHEMA
        or not isinstance(manifest["examples"], list)
        or len(manifest["examples"]) > MAX_ENTRIES
    ):
        raise VPError("Invalid evaluation manifest schema")
    result, ids, labels = [], set(), {}
    for example in manifest["examples"]:
        if not isinstance(example, dict) or set(example) != {
            "id",
            "audio",
            "sha256",
            "expected_text",
            "provenance",
            "label",
            "reviewer",
            "note",
        }:
            raise VPError("Invalid labeled-example schema")
        if not isinstance(example["id"], str) or not example["id"] or example["id"] in ids:
            raise VPError("Labeled examples need unique IDs")
        ids.add(example["id"])
        if not _valid_provenance(example["provenance"]):
            raise VPError("Labeled example provenance is incomplete")
        audio = _basename(manifest_path.parent, example["audio"])
        if not _hash(example["sha256"]) or _audio_hash(audio) != example["sha256"]:
            raise VPError("Labeled audio hash mismatch", code="needs_recovery")
        ident = fingerprint(_identity(example["sha256"], example["expected_text"], example["provenance"]))
        entry = {
            "schema_version": SCHEMA,
            "id": ident,
            "audio": f"{ident}.wav",
            "audio_sha256": example["sha256"],
            "expected_text": example["expected_text"],
            "provenance": example["provenance"],
            "label": example["label"],
            "review": {
                "reviewer": example["reviewer"],
                "note": example["note"],
                "at": datetime.now(UTC).isoformat(),
                "evidence_key": _evidence_key(
                    example["sha256"], example["expected_text"], example["provenance"]
                ),
            },
            "source": {"manifest_sha256": sha256(manifest_path), "example_id": example["id"]},
            "created_at": datetime.now(UTC).isoformat(),
            "trust_model": TRUST_MODEL,
        }
        _validate_entry(entry)
        if ident in labels and labels[ident] != entry["label"]:
            raise VPError("Contradictory labels for identical evaluation evidence")
        labels[ident] = entry["label"]
        result.append((entry, audio))
    return result


def import_labeled_examples(manifest_path: Path, corpus_dir: Path) -> dict:
    """Import explicit local listening labels; this is an assertion by the reviewer."""
    return _publish(Path(corpus_dir), _examples(manifest_path))


def evaluate(manifest_path: Path, corpus_dir: Path) -> dict:
    """Report retrieval coverage and label disagreement, without calibration claims."""
    examples = _examples(manifest_path)
    _, corpus_entries = _load(Path(corpus_dir).expanduser().resolve())
    by_id = {entry["id"]: entry for entry in corpus_entries}
    counts = {
        key: 0
        for key in (
            "accepted_labels",
            "rejected_labels",
            "uncertain_labels",
            "correct_accepts",
            "correct_rejects",
            "false_accepts",
            "false_rejects",
            "unknown",
            "exact_in_corpus_matches",
        )
    }
    results = []
    for entry, audio in examples:
        decision = _match_loaded(_audio_hash(audio), entry["expected_text"], entry["provenance"], by_id)
        label, prediction = entry["label"], decision["status"]
        counts[f"{label}_labels"] += 1
        if decision["reason"] == "exact_in_corpus_match":
            counts["exact_in_corpus_matches"] += 1
        if prediction == "unknown":
            counts["unknown"] += 1
        elif label == "accepted":
            counts["correct_accepts" if prediction == "accepted" else "false_rejects"] += 1
        elif label == "rejected":
            counts["false_accepts" if prediction == "accepted" else "correct_rejects"] += 1
        results.append({"example_id": entry["source"]["example_id"], "label": label, "decision": decision})
    return {
        "schema_version": SCHEMA,
        "method": METHOD,
        "trust_model": TRUST_MODEL,
        "total": len(examples),
        **counts,
        "results": results,
        "calibrated": False,
        "naturalness_inference": False,
        "interpretation": "Exact-reference retrieval and coverage only. In-corpus matches are not independent quality or naturalness validation.",
    }
