"""Synchronous production stages; all resumable state is inspectable JSON."""

from __future__ import annotations

import copy
import math
import time
from pathlib import Path

from . import jobs, lexicon, qa
from .common import VPError, atomic_json, fingerprint, sha256
from .decisions import apply_decisions
from .pptx import read_pptx
from .pronunciation import expected_reading, matched_entries
from .text import SPLIT_SCHEMA_VERSION, detect_candidates, ingest_text, plan_chunks
from .voicepeak import Voicepeak, engine_session

STATE_DIR = Path.home() / ".local/state/vp-manager/dictionary-transactions"


def engine_options(job: dict) -> dict:
    return {k: job["options"][k] for k in ("narrator", "speed", "pitch", "emotion")}


def analyze(
    directory: Path,
    engine: Voicepeak,
    *,
    source: Path | None = None,
    text: str | None = None,
    options: dict | None = None,
) -> dict:
    job = jobs.create(directory, source=source, text=text, options=options)
    source_copy = directory / job["source"]["copy"]
    if source_copy.suffix.lower() == ".pptx":
        job.update(read_pptx(source_copy))
    else:
        try:
            raw = source_copy.read_bytes().decode("utf-8")
        except UnicodeError as exc:
            raise VPError("Input text must use UTF-8") from exc
        job["units"] = ingest_text(raw)
    if not job["units"] and not job["slides"]:
        raise VPError("Input is empty")
    dictionary = lexicon.read_dictionary(engine.settings)
    job["candidates"] = detect_candidates(job["units"], dictionary)
    job["unresolved_candidates"] = [c["id"] for c in job["candidates"]]
    if job["candidates"]:
        job["status"] = "needs_decision"
    else:
        job["chunks"] = plan_chunks(job["units"])
        job["status"] = "planned"
    job["analysis_issues"] = copy.deepcopy(job["issues"])
    jobs.save(directory, job)
    return job


def decide(directory: Path, job: dict, payload: dict) -> dict:
    result = apply_decisions(job["units"], job["candidates"], job["source_revision"], payload)
    protected = [o["reading"] for o in payload.get("overrides", [])]
    protected += [e["sur"] for e in result["entries"]]
    chunks = plan_chunks(result["units"], protected=protected)
    job.update(
        units=result["units"],
        dictionary_entries=result["entries"],
        decisions=result["decisions"],
        unresolved_candidates=result["unresolved_candidates"],
        chunks=chunks,
    )
    # Artifacts and QA are derived. Render history is retained for budget and
    # unchanged-failure detection; only exact render keys may be reused later.
    job["artifacts"] = {}
    job["qa"] = {}
    job["quality"] = "unreviewed"
    job["status"] = "needs_decision" if result["unresolved_candidates"] else "planned"
    job["issues"] = copy.deepcopy(job.get("analysis_issues", []))
    atomic_json(directory / "decisions.json", payload)
    jobs.save(directory, job)
    return job


def configure(directory: Path, job: dict, changes: dict) -> dict:
    allowed = {
        "narrator",
        "speed",
        "pitch",
        "emotion",
        "paragraph_pause",
        "slide_lead",
        "slide_tail",
        "blank_duration",
    }
    if set(changes) - allowed:
        raise VPError("Unknown rendering option")
    options = {**job["options"], **changes}
    if not isinstance(options["narrator"], str) or not options["narrator"]:
        raise VPError("Narrator must be a nonempty string")
    if type(options["speed"]) is not int or not 50 <= options["speed"] <= 200:
        raise VPError("Speed must be 50–200")
    if type(options["pitch"]) is not int or not -300 <= options["pitch"] <= 300:
        raise VPError("Pitch must be -300–300")
    for key in ("paragraph_pause", "slide_lead", "slide_tail", "blank_duration"):
        if (
            type(options[key]) not in (int, float)
            or not math.isfinite(options[key])
            or not 0 <= options[key] <= 60
        ):
            raise VPError(f"{key} must be a finite number from 0 to 60 seconds")
    if not isinstance(options["emotion"], dict):
        raise VPError("Emotion must be an object")
    job["options"] = options
    job["artifacts"], job["qa"] = {}, {}
    job["quality"] = "unreviewed"
    job["status"] = "needs_decision" if job.get("unresolved_candidates") else "planned"
    jobs.save(directory, job)
    return job


def _plan_key(job: dict) -> str:
    return fingerprint(
        {
            "source": job["source_revision"],
            "chunks": [
                {k: c[k] for k in ("id", "unit_id", "text", "slide_id", "paragraph_index")}
                for c in job["chunks"]
            ],
            "entries": job["dictionary_entries"],
            "options": engine_options(job),
        }
    )


def current_renders(directory: Path, job: dict) -> None:
    if job.get("status") == "needs_recovery":
        raise VPError("Resolve dictionary or artifact recovery before continuing", code="needs_recovery")
    if job.get("render_plan_key") != _plan_key(job):
        raise VPError("Rendering is stale; render the current plan before this stage", code="needs_decision")
    keys = set()
    for chunk in job["chunks"]:
        record = job["renders"].get(chunk["id"])
        if not record or record.get("text") != chunk["text"] or record.get("status") != "complete":
            raise VPError(f"Missing current render: {chunk['id']}", code="needs_decision")
        expected_key = fingerprint({**job["render_snapshot"], "text": chunk["text"]})
        if (
            record.get("render_key") != expected_key
            or record.get("dictionary_hash") != job["render_snapshot"]["dictionary"]
        ):
            raise VPError("Rendering provenance differs from the completed batch", code="needs_recovery")
        path = directory / record["path"]
        if not path.is_file() or sha256(path) != record["sha256"]:
            raise VPError(f"Render hash mismatch: {chunk['id']}", code="needs_recovery")
        keys.add(record["dictionary_hash"])
    if len(keys) > 1:
        raise VPError("Mixed dictionary snapshots cannot be assembled", code="needs_recovery")


def render(directory: Path, job: dict, engine: Voicepeak) -> dict:
    if job.get("unresolved_candidates"):
        raise VPError("Resolve reading candidates before synthesis", code="needs_decision")
    options = engine_options(job)
    history = job["history"]
    with engine_session(engine.settings):
        lexicon.assert_clean(engine.settings, STATE_DIR)
        inventory = engine.inventory()
        # Preflight every chunk and voice setting before any synthesis or mutation.
        for chunk in job["chunks"]:
            engine.validate_input(chunk["text"], **options)
        dictionary_preview = lexicon.preview_dictionary(engine.settings, job["dictionary_entries"])
        pending_entries = dictionary_preview["pending_entries"]
        input_dictionary = lexicon.read_dictionary(engine.settings) + pending_entries
        with lexicon.transaction(engine.settings, pending_entries, STATE_DIR) as transaction:
            base = {
                "engine": inventory["version"],
                "assets": inventory["voice_asset_fingerprint"],
                "dictionary": transaction["dictionary_hash"],
                "split": SPLIT_SCHEMA_VERSION,
                **options,
            }
            pending = []
            seen_keys = set()
            reserved = {}
            logical_positions = {}
            unit_positions = {}
            for chunk in job["chunks"]:
                unit_positions[chunk["unit_id"]] = unit_positions.get(chunk["unit_id"], 0) + 1
                logical_positions[chunk["id"]] = f"{chunk['unit_id']}:{unit_positions[chunk['unit_id']]}"
            job.setdefault("unit_chunk_budget", {})
            for chunk in job["chunks"]:
                job["unit_chunk_budget"].setdefault(
                    chunk["unit_id"], sum(c["unit_id"] == chunk["unit_id"] for c in job["chunks"])
                )
            for chunk in job["chunks"]:
                key = fingerprint({**base, "text": chunk["text"]})
                chunk["render_key"] = key
                existing = next(
                    (
                        h
                        for h in reversed(history)
                        if h.get("render_key") == key and h.get("status") == "complete"
                    ),
                    None,
                )
                if (
                    existing
                    and (directory / existing["path"]).is_file()
                    and sha256(directory / existing["path"]) == existing["sha256"]
                ):
                    job["renders"][chunk["id"]] = {**existing, "text": chunk["text"]}
                    continue
                if any(h.get("render_key") == key and h["status"] in ("failed", "started") for h in history):
                    raise VPError(
                        "An unchanged failed or interrupted synthesis must be investigated before retry",
                        code="needs_decision",
                    )
                # Count by stable original paragraph, including obsolete chunk IDs.
                # Four complete versions of that paragraph are the maximum budget.
                count = sum(h.get("unit_id") == chunk["unit_id"] for h in history) + reserved.get(
                    chunk["unit_id"], 0
                )
                budget = job["options"]["max_attempts"] * job["unit_chunk_budget"][chunk["unit_id"]]
                logical_id = logical_positions[chunk["id"]]
                logical_count = sum(
                    h.get("logical_id") == logical_id
                    or (
                        not h.get("logical_id")
                        and h.get("unit_id") == chunk["unit_id"]
                        and h.get("chunk_id") == chunk["id"]
                    )
                    for h in history
                )
                if key not in seen_keys and (
                    count >= budget or logical_count >= job["options"]["max_attempts"]
                ):
                    raise VPError(
                        "Candidate attempt budget exhausted for source paragraph", code="needs_decision"
                    )
                if key not in seen_keys:
                    reserved[chunk["unit_id"]] = reserved.get(chunk["unit_id"], 0) + 1
                    seen_keys.add(key)
                pending.append((chunk, key))
            if (
                not pending
                and job.get("render_snapshot") == base
                and job.get("render_plan_key") == _plan_key(job)
            ):
                current_renders(directory, job)
                # A validated cache can acquire missing provenance from the same
                # live snapshot. Preserve the original request after promotion.
                job.setdefault("render_dictionary_base_hash", dictionary_preview["base_dictionary_hash"])
                job.setdefault("render_dictionary_entries", copy.deepcopy(pending_entries))
                job.setdefault(
                    "render_lexicon",
                    list(
                        {
                            m["entry"]["sur"]: m["entry"]
                            for c in job["chunks"]
                            for m in matched_entries(c["text"], input_dictionary)
                            if isinstance(m["entry"].get("pron"), str)
                        }.values()
                    ),
                )
                jobs.save(directory, job)
                return job
            job.pop("render_plan_key", None)
            job["status"] = "rendering"
            job["quality"] = "unreviewed"
            job["artifacts"], job["qa"] = {}, {}
            job["issues"] = copy.deepcopy(job.get("analysis_issues", []))
            jobs.save(directory, job)
            for chunk, key in pending:
                shared = next(
                    (
                        h
                        for h in reversed(history)
                        if h.get("render_key") == key and h["status"] == "complete"
                    ),
                    None,
                )
                if (
                    shared
                    and (directory / shared["path"]).is_file()
                    and sha256(directory / shared["path"]) == shared["sha256"]
                ):
                    job["renders"][chunk["id"]] = copy.deepcopy(shared)
                    continue
                spent = sum(
                    h.get("elapsed_seconds", h.get("timeout_seconds", 60) if h["status"] == "started" else 0)
                    for h in history
                )
                if spent >= job["options"]["max_seconds"]:
                    raise VPError("Job synthesis time budget exhausted", code="needs_decision")
                prior_timeout = getattr(engine, "timeout", 60)
                call_timeout = min(prior_timeout, job["options"]["max_seconds"] - spent)
                record = {
                    "timeout_seconds": call_timeout,
                    "unit_id": chunk["unit_id"],
                    "logical_id": logical_positions[chunk["id"]],
                    "chunk_id": chunk["id"],
                    "render_key": key,
                    "text": chunk["text"],
                    "path": f"audio/{key}.wav",
                    "status": "started",
                    "started_at": jobs.now(),
                    "dictionary_hash": base["dictionary"],
                }
                history.append(record)
                jobs.save(directory, job)  # A hard kill consumes an attempt too.
                started = time.monotonic()
                engine.timeout = call_timeout
                try:
                    metrics = engine.render(chunk["text"], directory / record["path"], **options)
                    if metrics["dictionary_hash"] != base["dictionary"]:
                        raise VPError(
                            "Dictionary identity changed within render batch", code="needs_recovery"
                        )
                    record.update({k: v for k, v in metrics.items() if k != "path"})
                    record["status"] = "complete"
                    job["renders"][chunk["id"]] = copy.deepcopy(record)
                except BaseException as exc:
                    record.update(status="failed", error=str(exc), elapsed_seconds=time.monotonic() - started)
                    jobs.save(directory, job)
                    raise
                finally:
                    engine.timeout = prior_timeout
                jobs.save(directory, job)
            job["render_dictionary_base_hash"] = dictionary_preview["base_dictionary_hash"]
            job["render_dictionary_entries"] = copy.deepcopy(pending_entries)
            job["render_lexicon"] = list(
                {
                    m["entry"]["sur"]: m["entry"]
                    for c in job["chunks"]
                    for m in matched_entries(c["text"], input_dictionary)
                    if isinstance(m["entry"].get("pron"), str)
                }.values()
            )
            job["render_snapshot"] = base
            job["render_plan_key"] = _plan_key(job)
            job["status"] = "checking"
            jobs.save(directory, job)
    return job


def verify(directory: Path, job: dict, model: Path | None = None, corpus: Path | None = None) -> dict:
    current_renders(directory, job)
    issues = copy.deepcopy(job.get("analysis_issues", []))
    results = {}
    previous_qa = copy.deepcopy(job.get("qa", {}))
    if corpus is not None:
        job["reference_corpus"] = str(corpus.expanduser().resolve())
    for chunk in job["chunks"]:
        record = job["renders"][chunk["id"]]
        path = directory / record["path"]
        old = previous_qa.get(chunk["id"], {})
        transcript = (
            old.get("content", {}).get("transcript")
            if old.get("acoustics", {}).get("sha256") == record["sha256"]
            else None
        )
        result = qa.check(
            path,
            chunk["text"],
            model=model,
            transcript=transcript,
            acceptance=_review_acceptance(directory, job, chunk),
            content_expected=expected_reading(chunk["text"], job.get("render_lexicon", [])),
            provenance=job["render_snapshot"],
        )
        results[chunk["id"]] = result
        issues.extend({**entry, "chunk_id": chunk["id"]} for entry in result["issues"])
        job["qa"] = results
        jobs.save(directory, job)
    job["qa"] = results
    job["quality"] = "verified" if all(r["status"] == "pass" for r in results.values()) else "draft"
    job["status"] = "checked" if job["quality"] == "verified" else "needs_revision"
    job["issues"] = issues
    if model:
        job["asr_model"] = str(model.resolve())
    atomic_json(directory / "qa.json", results)
    jobs.save(directory, job)
    return job


def accept(directory: Path, job: dict, chunk_id: str, reviewer: str, note: str) -> dict:
    current_renders(directory, job)
    result = job.get("qa", {}).get(chunk_id)
    if not result or result["acoustics"]["status"] != "pass" or not result.get("evidence_key"):
        raise VPError("Run QA first; failed or suspect acoustics cannot be accepted")
    if not reviewer.strip() or not note.strip():
        raise VPError("Listening acceptance requires a reviewer and an evidence note")
    job["acceptances"][chunk_id] = {
        "evidence_key": result["evidence_key"],
        "reviewer": reviewer,
        "note": note,
        "at": jobs.now(),
    }
    return verify(directory, job)


def assemble(directory: Path, job: dict, allow_draft: bool = False) -> dict:
    from .media import assemble as assemble_media

    current_renders(directory, job)
    result = assemble_media(directory, job, allow_draft)
    job["assembly"] = result
    job["artifacts"]["audio"] = result["audio"]
    job["artifacts"]["slide_audio"] = result["slides"]
    job["quality"] = result["quality"]
    job["status"] = "verified" if result["quality"] == "verified" and not job["slides"] else "assembled"
    jobs.save(directory, job)
    return job


def export_video(directory: Path, job: dict, **options) -> dict:
    from .media import export_video as export_media

    current_renders(directory, job)
    result = export_media(directory, job, **options)
    job["video_export"] = result
    job["quality"] = result["quality"]
    job["artifacts"]["video"] = result["video"]
    job["status"] = "exported"
    jobs.save(directory, job)
    return job


def _review_acceptance(directory: Path, job: dict, chunk: dict) -> dict | None:
    explicit = job.get("acceptances", {}).get(chunk["id"])
    if explicit:
        return explicit
    corpus = job.get("reference_corpus")
    if not corpus:
        return None
    from .references import match_reference, reference_provenance

    match = match_reference(
        directory / job["renders"][chunk["id"]]["path"],
        chunk["text"],
        reference_provenance(job, chunk),
        Path(corpus),
    )
    job.setdefault("reference_matches", {})[chunk["id"]] = match
    return match.get("acceptance") if match["status"] == "accepted" else None


def promote_dictionary(directory: Path, job: dict, engine: Voicepeak) -> dict:
    """Publish only the actual tested entries with current, recorded listening evidence."""
    current_renders(directory, job)
    if sha256(directory / job["source"]["copy"]) != job["source_revision"]:
        raise VPError("Original copy changed", code="needs_recovery")
    entries = job.get("render_dictionary_entries")
    baseline = job.get("render_dictionary_base_hash")
    if not entries or not baseline:
        raise VPError(
            "No tested additions with a recorded baseline; render new entries first", code="needs_decision"
        )
    requested = [lexicon.validate_entry(e) for e in job["dictionary_entries"]]
    if any(e not in requested for e in entries):
        raise VPError("Dictionary decisions differ from the tested additions", code="needs_decision")
    with engine_session(engine.settings):
        lexicon.assert_clean(engine.settings, STATE_DIR)
        inventory = engine.inventory()
        snapshot = job["render_snapshot"]
        if (
            inventory["version"] != snapshot["engine"]
            or inventory["voice_asset_fingerprint"] != snapshot["assets"]
        ):
            raise VPError("Engine or voice assets changed since listening review", code="needs_decision")
        # All occurrences of every new entry must be in reviewed audio; an unused
        # entry cannot acquire approval merely by being included in a batch.
        eligible = {e["sur"]: [] for e in entries}
        proofs = []
        all_entries = job.get("render_lexicon", entries)
        for chunk in job["chunks"]:
            used = {m["entry"]["sur"] for m in matched_entries(chunk["text"], all_entries)} & eligible.keys()
            if not used:
                continue
            record = job["renders"][chunk["id"]]
            acceptance = _review_acceptance(directory, job, chunk)
            checked = qa.check(
                directory / record["path"],
                chunk["text"],
                acceptance=acceptance,
                content_expected=expected_reading(chunk["text"], all_entries),
                provenance=snapshot,
            )
            if checked["status"] != "pass" or checked.get("listening") != "accepted":
                raise VPError(
                    f"Listening acceptance required for dictionary audio: {chunk['id']}",
                    code="needs_decision",
                )
            for surface in used:
                eligible[surface].append(chunk["id"])
            proofs.append(
                {
                    "chunk_id": chunk["id"],
                    "text": chunk["text"],
                    "audio_sha256": record["sha256"],
                    "render_key": record["render_key"],
                    "evidence_key": checked["evidence_key"],
                    "acceptance": copy.deepcopy(acceptance),
                }
            )
        missing = [surface for surface, chunks in eligible.items() if not chunks]
        if missing:
            raise VPError(
                "Dictionary entries were not used in synthesized text: " + ", ".join(missing),
                code="needs_decision",
            )
        evidence = {
            "schema_version": 1,
            "job_id": job["job_id"],
            "source_revision": job["source_revision"],
            "decisions_hash": fingerprint(job.get("decisions", {})),
            "plan_key": _plan_key(job),
            "render_snapshot": snapshot,
            "coverage": eligible,
            "chunks": proofs,
        }
        request_id = fingerprint({"base": baseline, "entries": entries, "evidence": evidence})
        previous = job.get("dictionary_promotion_request")
        if previous and previous["id"] == request_id:
            evidence = previous["evidence"]
        else:
            preview = lexicon.preview_dictionary(engine.settings, entries)
            if (
                preview["base_dictionary_hash"] != baseline
                or preview["dictionary_hash"] != snapshot["dictionary"]
            ):
                raise VPError(
                    "Dictionary baseline or tested snapshot changed; render and review again",
                    code="needs_decision",
                )
            job["dictionary_promotion_request"] = {
                "id": request_id,
                "base": baseline,
                "entries": copy.deepcopy(entries),
                "evidence": evidence,
            }
            jobs.save(directory, job)  # Preserve retry identity before external mutation.
        receipt = lexicon.promote(
            engine.settings, entries, STATE_DIR, expected_base_hash=baseline, evidence=evidence
        )
        job["dictionary_promotion"] = receipt
        jobs.save(directory, job)
    return job


def export_references(directory: Path, job: dict, destination: Path) -> dict:
    from .references import export_reference

    current_renders(directory, job)
    result = export_reference(directory, job, destination)
    job["reference_export"] = result
    jobs.save(directory, job)
    return job


def prepare_slides(directory: Path, job: dict) -> dict:
    from .render_copy import prepare

    if not job["slides"]:
        raise VPError("Slide preparation requires a PPTX input")
    result = prepare(directory / job["source"]["copy"], directory / "rendering/all-slides.pptx")
    job["render_copy"] = result
    job["artifacts"]["render_copy"] = result
    jobs.save(directory, job)
    return job
