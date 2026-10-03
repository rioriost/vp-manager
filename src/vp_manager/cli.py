"""Machine-readable local CLI. No command executes agent-supplied code."""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

from . import __version__, jobs, lexicon, pipeline, references
from .common import VPError, issue
from .voicepeak import Voicepeak, engine_session

EXIT_CODES = {
    "input": 2,
    "environment": 3,
    "busy": 3,
    "needs_decision": 4,
    "synthesis": 5,
    "needs_recovery": 6,
}


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="vp-manager", description="Local VOICEPEAK audio production")
    root.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    root.add_argument("--executable", type=Path, help="Custom engine executable (test adapters)")
    root.add_argument("--settings", type=Path, help="Custom engine settings (test adapters)")
    root.add_argument("--timeout", type=float, default=60)
    commands = root.add_subparsers(dest="command", required=True)
    p = commands.add_parser("install-skill", help="Install the bundled production Skill for this user")
    p.add_argument("--dry-run", action="store_true", help="Show installation plan without writing files")
    p.add_argument("--json", action="store_true")
    for name in ("doctor", "recover-dictionary"):
        p = commands.add_parser(name)
        p.add_argument("--json", action="store_true", help="Structured JSON (also the default)")
    for name in ("reference-list", "reference-import", "evaluate-references"):
        p = commands.add_parser(name)
        p.add_argument("--corpus", required=True, type=Path)
        p.add_argument("--json", action="store_true")
        if name != "reference-list":
            p.add_argument("--file", required=True, type=Path)
    p = commands.add_parser("analyze")
    p.add_argument("input", nargs="?", type=Path)
    p.add_argument("--text")
    p.add_argument("--job", required=True, type=Path)
    p.add_argument("--narrator", default="Japanese Female 1")
    p.add_argument("--speed", type=int, default=100)
    p.add_argument("--pitch", type=int, default=0)
    p.add_argument("--json", action="store_true")
    for name in (
        "status",
        "render",
        "resume",
        "verify",
        "assemble",
        "export-video",
        "apply-decisions",
        "configure",
        "accept-review",
        "promote-dictionary",
        "export-references",
        "prepare-slides",
    ):
        p = commands.add_parser(name)
        p.add_argument("job", type=Path)
        p.add_argument("--json", action="store_true")
        if name in ("apply-decisions", "configure"):
            p.add_argument("--file", type=Path, required=True)
        if name == "verify":
            p.add_argument("--asr-model", type=Path)
            p.add_argument("--corpus", type=Path)
        if name == "export-references":
            p.add_argument("--corpus", required=True, type=Path)
        if name in ("assemble", "export-video"):
            p.add_argument("--allow-draft", action="store_true")
        if name == "export-video":
            render = p.add_mutually_exclusive_group()
            render.add_argument("--pdf", type=Path, help="PDF containing every slide including hidden slides")
            render.add_argument(
                "--soffice", type=Path, help="Explicit bundled headless LibreOffice executable"
            )
            p.add_argument(
                "--font-dir",
                type=Path,
                action="append",
                help="Read-only font directory for bundled renderer; repeatable",
            )
            p.add_argument("--include-hidden", action="store_true")
            p.add_argument("--fps", type=int, default=25)
            p.add_argument(
                "--resolution",
                default="1080p",
                help="Output canvas: 720p, 1080p (default), 2160p, or even WIDTHxHEIGHT",
            )
            p.add_argument(
                "--duck-db",
                type=float,
                default=-18.0,
                help="Embedded video audio attenuation during narration, -60 to 0 dB (default: -18)",
            )
        if name == "accept-review":
            p.add_argument("--chunk", required=True)
            p.add_argument("--reviewer", required=True)
            p.add_argument("--note", required=True)
    return root


def emit(result: dict) -> None:
    print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    job = None
    directory = None
    try:
        if args.command == "install-skill":
            from .skill_install import install_skill

            result = install_skill(dry_run=args.dry_run)
            emit({"schema_version": 1, "job_id": None, **result})
            return 3 if result["status"] == "pending_activation" else 0
        if args.command in ("reference-list", "reference-import", "evaluate-references"):
            if args.command == "reference-list":
                result = references.index_reference(args.corpus)
            elif args.command == "reference-import":
                result = references.import_labeled_examples(args.file, args.corpus)
            else:
                result = references.evaluate(args.file, args.corpus)
            emit({"schema_version": 1, "job_id": None, "status": "complete", **result})
            return 0
        engine = Voicepeak(args.executable, args.settings, args.timeout)
        if args.command in ("doctor", "recover-dictionary"):
            with engine_session(engine.settings):
                if args.command == "recover-dictionary":
                    result = lexicon.recover(engine.settings, pipeline.STATE_DIR)
                    emit(
                        {
                            "schema_version": 1,
                            "job_id": None,
                            **result,
                            "artifacts": {},
                            "issues": [],
                            "next_action": "Retry pending job after recovery",
                        }
                    )
                    return 0
                lexicon.assert_clean(engine.settings, pipeline.STATE_DIR)
                inventory = engine.inventory()
                emit(
                    {
                        "schema_version": 1,
                        "job_id": None,
                        "status": "ready",
                        "inventory": inventory,
                        "artifacts": {},
                        "issues": [],
                        "tools": {n: shutil.which(n) for n in ("ffmpeg", "ffprobe", "pdfinfo", "pdftoppm")},
                        "next_action": "analyze; this check does not certify voice quality",
                    }
                )
                return 0
        directory = args.job.expanduser().resolve()
        if args.command == "analyze":
            if (args.input is None) == (args.text is None):
                raise VPError("Supply exactly one input file or --text")
            source = args.input.expanduser().resolve() if args.input else None
            job = pipeline.analyze(
                directory,
                engine,
                source=source,
                text=args.text,
                options={"narrator": args.narrator, "speed": args.speed, "pitch": args.pitch},
            )
            emit(
                {
                    **jobs.public(job),
                    "job_directory": str(directory),
                    "candidates": job["candidates"],
                    "slides": job["slides"],
                    "units": job["units"],
                }
            )
            return 4 if job["status"] == "needs_decision" else 0
        with jobs.locked(directory) as job:
            try:
                if args.command == "status":
                    emit(
                        {
                            **jobs.public(job),
                            "job_directory": str(directory),
                            "chunks": job["chunks"],
                            "unresolved_candidates": job.get("unresolved_candidates", []),
                            "qa": job.get("qa", {}),
                        }
                    )
                    return 0
                if args.command in ("apply-decisions", "configure"):
                    payload = json.loads(args.file.read_text(encoding="utf-8"))
                    if args.command == "apply-decisions":
                        pipeline.decide(directory, job, payload)
                    else:
                        if not isinstance(payload, dict):
                            raise VPError("Options file must contain a JSON object")
                        pipeline.configure(directory, job, payload)
                elif args.command in ("render", "resume"):
                    # resume renders only when required; never implicitly certifies quality.
                    pipeline.render(directory, job, engine)
                elif args.command == "verify":
                    pipeline.verify(directory, job, args.asr_model, args.corpus)
                elif args.command == "promote-dictionary":
                    pipeline.promote_dictionary(directory, job, engine)
                elif args.command == "export-references":
                    pipeline.export_references(directory, job, args.corpus)
                elif args.command == "prepare-slides":
                    pipeline.prepare_slides(directory, job)
                elif args.command == "accept-review":
                    pipeline.accept(directory, job, args.chunk, args.reviewer, args.note)
                elif args.command == "assemble":
                    pipeline.assemble(directory, job, args.allow_draft)
                elif args.command == "export-video":
                    pipeline.export_video(
                        directory,
                        job,
                        pdf=args.pdf,
                        soffice=args.soffice,
                        allow_draft=args.allow_draft,
                        include_hidden=args.include_hidden,
                        font_dirs=args.font_dir,
                        fps=args.fps,
                        resolution=args.resolution,
                        duck_db=args.duck_db,
                    )
            except VPError as exc:
                if args.command != "status":
                    job["status"] = exc.code if exc.code in ("needs_decision", "needs_recovery") else "failed"
                    job["issues"] = [*job.get("issues", []), issue(exc.code, str(exc), "error")]
                    jobs.save(directory, job)
                raise
            except KeyboardInterrupt:
                job["status"] = "cancelled"
                jobs.save(directory, job)
                raise
            emit(jobs.public(job))
            return 4 if job["status"] in ("needs_decision", "needs_revision") else 0
    except KeyboardInterrupt:
        emit(
            {
                "schema_version": 1,
                "job_id": job.get("job_id") if job else None,
                "status": "cancelled",
                "artifacts": job.get("artifacts", {}) if job else {},
                "issues": [],
                "next_action": "Inspect status and dictionary recovery journal before resuming",
            }
        )
        return 130
    except (VPError, OSError, ValueError) as exc:
        code = exc.code if isinstance(exc, VPError) else "input"
        emit(
            {
                "schema_version": 1,
                "job_id": job.get("job_id") if job else None,
                "status": code if code.startswith("needs_") else "failed",
                "artifacts": job.get("artifacts", {}) if job else {},
                "issues": [issue(code, str(exc), "error")],
                "next_action": "Inspect issue before retrying",
            }
        )
        return EXIT_CODES.get(code, 1)


if __name__ == "__main__":
    sys.exit(main())
