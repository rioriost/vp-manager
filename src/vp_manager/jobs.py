"""Private immutable inputs and durable, inspectable job manifests."""

from __future__ import annotations

import json
import os
import shutil
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from .common import VPError, atomic_json, file_lock, sha256

SCHEMA = 1


def now() -> str:
    return datetime.now(UTC).isoformat()


def save(directory: Path, job: dict) -> None:
    job["updated_at"] = now()
    atomic_json(Path(directory) / "job.json", job)


def load(directory: Path) -> dict:
    directory = Path(directory)
    try:
        job = json.loads((directory / "job.json").read_text())
    except (OSError, ValueError) as exc:
        raise VPError(f"Cannot read job manifest: {exc}") from exc
    if job.get("schema_version") != SCHEMA:
        raise VPError("Unsupported job schema")
    source = directory / job["source"]["copy"]
    if not source.is_file() or sha256(source) != job["source_revision"]:
        raise VPError("Immutable source copy has changed", code="needs_recovery")
    return job


@contextmanager
def locked(directory: Path):
    directory = Path(directory).expanduser().resolve()
    if not directory.is_dir():
        raise VPError(f"Job directory does not exist: {directory}")
    with file_lock(directory / ".job.lock"):
        yield load(directory)


def create(
    directory: Path, *, source: Path | None = None, text: str | None = None, options: dict | None = None
) -> dict:
    directory = Path(directory).expanduser().resolve()
    if directory.exists() and any(directory.iterdir()):
        raise VPError("Use a new empty job directory; analyze never overwrites an existing job")
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(directory, 0o700)
    suffix = source.suffix.lower() if source else ".txt"
    copy = directory / f"source{suffix}"
    if source:
        if suffix not in (".txt", ".md", ".pptx"):
            raise VPError("Input must be UTF-8 text, Markdown, or PPTX")
        shutil.copyfile(source, copy)
    else:
        copy.write_text(text or "", encoding="utf-8")
    os.chmod(copy, 0o600)
    return {
        "schema_version": SCHEMA,
        "job_id": str(uuid.uuid4()),
        "created_at": now(),
        "source_revision": sha256(copy),
        "status": "analyzed",
        "source": {"copy": copy.name, "original": str(source) if source else None},
        "options": {
            "narrator": "Japanese Female 1",
            "speed": 100,
            "pitch": 0,
            "emotion": {},
            "paragraph_pause": 0.4,
            "slide_lead": 0.25,
            "slide_tail": 0.5,
            "blank_duration": 3.0,
            "max_attempts": 4,
            "max_seconds": 3600,
            **(options or {}),
        },
        "units": [],
        "slides": [],
        "chunks": [],
        "candidates": [],
        "dictionary_entries": [],
        "renders": {},
        "history": [],
        "issues": [],
        "artifacts": {},
        "qa": {},
        "acceptances": {},
    }


def public(job: dict) -> dict:
    actions = {
        "needs_decision": "Write source-bound decisions, then apply-decisions",
        "planned": "render",
        "rendering": "resume",
        "checking": "verify",
        "needs_revision": "Inspect QA, revise readings or obtain listening acceptance; draft assembly is available",
        "checked": "assemble",
        "assembled": "export-video for PPTX, or review final audio",
        "exported": "Inspect video frames/timing and review unresolved audio quality",
        "verified": "Outputs passed configured checks and recorded listening review",
        "needs_recovery": "recover-dictionary, inspect preserved files before retrying",
        "failed": "Inspect failed_chunks and diagnostics; revise affected readings, then resume",
    }
    return {
        "schema_version": SCHEMA,
        "job_id": job["job_id"],
        "status": job["status"],
        "source_revision": job["source_revision"],
        "artifacts": job.get("artifacts", {}),
        "issues": job.get("issues", []),
        "next_action": actions.get(job["status"], "inspect status"),
        "job_file": "job.json",
        "quality": job.get("quality", "unreviewed"),
        **{
            key: job[key]
            for key in ("dictionary_promotion", "reference_export", "reference_matches",
                        "failed_chunks", "render_progress")
            if key in job
        },
    }
