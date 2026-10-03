"""Sample-preserving narration assembly and static/timed presentation video export."""

from __future__ import annotations

import json
import math
import os
import re
import shutil
import subprocess
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path

import numpy as np
import soundfile as sf

from .common import VPError, atomic_json, sha256
from .pronunciation import expected_reading
from .qa import evidence_key

BUNDLED_SOFFICE = (
    Path.home() / ".cache/codex-runtimes/codex-primary-runtime/dependencies/bin/override/soffice"
)


def _inside(directory: Path, relative: str) -> Path:
    if not isinstance(relative, str) or Path(relative).is_absolute():
        raise VPError("Artifact paths must be relative to the job")
    path = (directory / relative).resolve()
    if not path.is_relative_to(directory.resolve()):
        raise VPError("Artifact path escapes the job")
    return path


def _seconds(options: dict, key: str, default: float) -> float:
    value = options.get(key, default)
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or not 0 <= value <= 3600
    ):
        raise VPError(f"Invalid nonnegative duration: {key}")
    return float(value)


def _write_wav(directory: Path, relative: str, samples: np.ndarray, rate: int, subtype: str) -> dict:
    path = _inside(directory, relative)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(prefix=".audio-", suffix=".wav", dir=path.parent)
    os.close(fd)
    try:
        sf.write(temporary, samples, rate, subtype=subtype, format="WAV")
        with open(temporary, "rb") as stream:
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)
    return {
        "path": relative,
        "sha256": sha256(path),
        "frames": len(samples),
        "sample_rate": rate,
        "channels": samples.shape[1],
        "duration": len(samples) / rate,
        "subtype": subtype,
    }


def _load_chunks(directory: Path, job: dict, allow_draft: bool) -> tuple[dict, tuple, str]:
    data, formats, states = {}, set(), []
    chunks = job.get("chunks", [])
    if len({chunk["id"] for chunk in chunks}) != len(chunks):
        raise VPError("Duplicate chunk IDs")
    for chunk in chunks:
        render = job.get("renders", {}).get(chunk["id"])
        report = job.get("qa", {}).get(chunk["id"])
        if not render or not report or report.get("status") not in {"pass", "uncertain", "fail"}:
            raise VPError(f"Missing rendering or QA: {chunk['id']}", code="needs_decision")
        path = _inside(directory, render.get("path"))
        if not path.is_file() or sha256(path) != render.get("sha256"):
            raise VPError(f"Rendered audio changed: {chunk['id']}", code="needs_recovery")
        evidence_options = (
            {
                "content_expected": expected_reading(chunk["text"], job.get("render_lexicon", [])),
                "provenance": job["render_snapshot"],
            }
            if "render_snapshot" in job
            else {}
        )
        if report.get("evidence_key") and report["evidence_key"] != evidence_key(
            render["sha256"], chunk["text"], **evidence_options
        ):
            raise VPError(f"QA evidence is stale: {chunk['id']}", code="needs_decision")
        if report.get("acoustics", {}).get("sha256") not in (None, render["sha256"]):
            raise VPError(f"QA audio hash is stale: {chunk['id']}", code="needs_decision")
        if report["status"] == "fail":
            raise VPError(f"Failed QA cannot be assembled: {chunk['id']}", code="needs_decision")
        if report["status"] == "uncertain" and not allow_draft:
            raise VPError("Uncertain QA requires explicit --allow-draft", code="needs_decision")
        try:
            info = sf.info(path)
            samples, rate = sf.read(path, dtype="float64", always_2d=True)
        except (OSError, RuntimeError) as exc:
            raise VPError(f"Unreadable chunk WAV: {exc}") from exc
        if info.format not in {"WAV", "WAVEX"} or info.subtype not in {
            "PCM_16",
            "PCM_24",
            "PCM_32",
            "FLOAT",
            "DOUBLE",
        }:
            raise VPError("Assembly requires uncompressed PCM or floating-point WAV")
        if not len(samples) or not np.isfinite(samples).all() or samples.shape[1] not in (1, 2):
            raise VPError("Invalid samples or channel count")
        formats.add((rate, samples.shape[1], info.subtype))
        data[chunk["id"]] = samples
        states.append(report["status"])
    if len(formats) > 1:
        raise VPError("Chunk sample rates, channels and sample formats must match; no resampling is applied")
    audio_format = next(iter(formats), (48000, 1, "PCM_24"))
    if not 8000 <= audio_format[0] <= 192000:
        raise VPError("Unsupported sample rate")
    return data, audio_format, "draft" if "uncertain" in states else "verified"


def assemble(job_dir: Path, job: dict, allow_draft: bool = False) -> dict:
    """Add silence only: original samples, order and paragraph identities survive."""
    directory = Path(job_dir).resolve()
    data, (rate, channels, original_subtype), quality = _load_chunks(directory, job, allow_draft)
    subtype = "PCM_24" if original_subtype in {"PCM_16", "PCM_24"} else original_subtype
    options = job.get("options", {})
    pause = round(_seconds(options, "paragraph_pause", 0.4) * rate)
    lead = round(_seconds(options, "slide_lead", 0.25) * rate)
    tail = round(_seconds(options, "slide_tail", 0.5) * rate)
    blank = round(_seconds(options, "blank_duration", 3.0) * rate)
    if blank < 1:
        raise VPError("Blank slide duration must contain at least one audio sample")

    def silence(frames):
        return np.zeros((frames, channels), dtype=np.float64)

    def join(chunks):
        pieces, boundaries, offset, previous = [], [], 0, None
        for chunk in chunks:
            if previous is not None and previous != chunk["unit_id"]:
                pieces.append(silence(pause))
                offset += pause
            samples = data[chunk["id"]]
            boundaries.append(
                {
                    "chunk_id": chunk["id"],
                    "unit_id": chunk["unit_id"],
                    "start_sample": offset,
                    "end_sample": offset + len(samples),
                }
            )
            pieces.append(samples)
            offset += len(samples)
            previous = chunk["unit_id"]
        return np.concatenate(pieces) if pieces else silence(0), boundaries

    slides = job.get("slides", [])
    if not slides:
        samples, boundaries = join(job.get("chunks", []))
        if not len(samples):
            raise VPError("No narration chunks to assemble", code="needs_decision")
        audio = _write_wav(directory, "audio/narration.wav", samples, rate, subtype)
        audio["chunks"] = boundaries
        return {"audio": audio, "slides": [], "quality": quality}
    ids = [slide["id"] for slide in slides]
    if any(
        not isinstance(value, str) or not value.isascii() or not value.isdecimal() for value in ids
    ) or len(set(ids)) != len(ids):
        raise VPError("Slide IDs must be unique ASCII decimal strings")
    if any(chunk.get("slide_id") not in ids for chunk in job.get("chunks", [])):
        raise VPError("Narration chunk refers to an unknown slide")
    records, visible, presentation_boundaries, offset = [], [], [], 0
    for slide in slides:
        samples, boundaries = join(
            [chunk for chunk in job.get("chunks", []) if chunk.get("slide_id") == slide["id"]]
        )
        empty = not len(samples)
        if empty:
            samples = silence(blank)
        else:
            samples = np.concatenate([silence(lead), samples, silence(tail)])
            for boundary in boundaries:
                boundary["start_sample"] += lead
                boundary["end_sample"] += lead
        record = _write_wav(directory, f"audio/slide-{slide['id']}.wav", samples, rate, subtype)
        record.update(
            {
                "slide_id": slide["id"],
                "hidden": slide.get("hidden", False),
                "order": slide["order"],
                "empty_notes": empty,
                "chunks": boundaries,
            }
        )
        records.append(record)
        if not slide.get("hidden", False):
            visible.append(samples)
            presentation_boundaries.append(
                {"slide_id": slide["id"], "start_sample": offset, "end_sample": offset + len(samples)}
            )
            offset += len(samples)
    audio = (
        _write_wav(directory, "audio/presentation.wav", np.concatenate(visible), rate, subtype)
        if visible
        else None
    )
    if audio:
        audio["slides"] = presentation_boundaries
    return {
        "audio": audio,
        "slides": records,
        "quality": quality,
        "warnings": [] if visible else ["no_visible_slides"],
    }


def _run(
    arguments: list[str], *, cwd: Path | None = None, timeout: int = 300, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess:
    try:
        result = subprocess.run(
            arguments, cwd=cwd, capture_output=True, text=True, timeout=timeout, check=False, env=env
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise VPError(f"Media subprocess failed: {exc}", code="environment") from exc
    if result.returncode:
        raise VPError(
            f"Media subprocess exited {result.returncode}: {result.stderr[-3000:]}", code="environment"
        )
    return result


def _binary(name: str) -> str:
    path = shutil.which(name) or (
        f"/opt/homebrew/bin/{name}" if Path(f"/opt/homebrew/bin/{name}").is_file() else None
    )
    if not path:
        raise VPError(f"{name} is required for presentation video export", code="environment")
    return path


def _font_environment(temporary: Path, font_dirs: list[Path] | None) -> dict[str, str] | None:
    """Discover explicitly supplied fonts without installing/copying or changing global config."""
    if not font_dirs:
        return None
    config = ET.Element("fontconfig")
    for path in dict.fromkeys(Path(value).expanduser().resolve() for value in font_dirs):
        if not path.is_dir():
            raise VPError(f"Font directory does not exist: {path}", code="environment")
        ET.SubElement(config, "dir").text = str(path)
    cache = temporary / "font-cache"
    cache.mkdir(mode=0o700)
    ET.SubElement(config, "cachedir").text = str(cache)
    config_path = temporary / "fonts.conf"
    ET.ElementTree(config).write(config_path, encoding="utf-8", xml_declaration=True)
    environment = os.environ.copy()
    environment["FONTCONFIG_FILE"] = str(config_path)
    environment["FONTCONFIG_PATH"] = str(temporary)
    return environment


def _pdf_from_pptx(
    directory: Path, job: dict, executable: Path, temporary: Path, font_dirs: list[Path] | None = None
) -> Path:
    if Path(executable).expanduser().resolve() != BUNDLED_SOFFICE.resolve():
        raise VPError(
            "Only the explicitly supplied Codex bundled soffice is supported; desktop LibreOffice is not used",
            code="environment",
        )
    source = _inside(directory, job["source"]["copy"])
    if source.suffix.lower() != ".pptx" or not source.is_file() or sha256(source) != job["source_revision"]:
        raise VPError("PPTX source changed or is unavailable", code="needs_recovery")
    environment = _font_environment(temporary, font_dirs)
    copy = temporary / "source.pptx"
    shutil.copyfile(source, copy)
    out = temporary / "pdf"
    out.mkdir()
    profile = (temporary / "lo-profile").as_uri()
    properties = {
        "ExportHiddenSlides": {"type": "boolean", "value": "true"},
        "ExportNotesPages": {"type": "boolean", "value": "false"},
        "UseTransitionEffects": {"type": "boolean", "value": "false"},
    }
    _run(
        [
            str(executable),
            f"-env:UserInstallation={profile}",
            "--headless",
            "--nologo",
            "--nodefault",
            "--nofirststartwizard",
            "--convert-to",
            "pdf:impress_pdf_Export:" + json.dumps(properties, separators=(",", ":")),
            "--outdir",
            str(out),
            str(copy),
        ],
        timeout=180,
        env=environment,
    )
    if sha256(source) != job["source_revision"]:
        raise VPError("PPTX source changed during PDF conversion", code="needs_recovery")
    pdf = out / "source.pdf"
    if not pdf.is_file():
        raise VPError("Bundled soffice returned without creating the expected PDF", code="environment")
    return pdf


def _pdf_metadata(path: Path, page: int) -> dict:
    """Query the external Poppler tool; no PDF library is linked into Python."""
    environment = {**os.environ, "LC_ALL": "C", "LANG": "C"}
    info = _run(
        [_binary("pdfinfo"), "-f", str(page + 1), "-l", str(page + 1), "-box", str(path)],
        env=environment,
        timeout=60,
    ).stdout
    count = re.search(r"^Pages:\s+(\d+)\s*$", info, re.MULTILINE)
    encrypted = re.search(r"^Encrypted:\s+(\w+)", info, re.MULTILINE)
    box = re.search(
        r"^Page\s+\d+\s+CropBox:\s+([-+.\d]+)\s+([-+.\d]+)\s+([-+.\d]+)\s+([-+.\d]+)", info, re.MULTILINE
    )
    rotation = re.search(r"^Page\s+\d+\s+rot:\s+(-?\d+)", info, re.MULTILINE)
    if not count or not encrypted or not box or not rotation:
        raise VPError("Poppler returned incomplete PDF page metadata", code="environment")
    x0, y0, x1, y1 = map(float, box.groups())
    width, height = x1 - x0, y1 - y0
    if int(rotation[1]) % 180:
        width, height = height, width
    if not all(math.isfinite(value) and value > 0 for value in (width, height)):
        raise VPError("Invalid PDF page dimensions")
    return {"pages": int(count[1]), "encrypted": encrypted[1] != "no", "width": width, "height": height}


def parse_resolution(resolution: str = "1080p") -> tuple[int, int]:
    """Resolve a named or explicit square-pixel output canvas, including portrait."""
    presets = {"720p": (1280, 720), "1080p": (1920, 1080), "2160p": (3840, 2160)}
    if isinstance(resolution, str):
        value = resolution.strip().lower()
        if value in presets:
            return presets[value]
        match = re.fullmatch(r"([0-9]{1,4})x([0-9]{1,4})", value)
        if match:
            width, height = map(int, match.groups())
            if all(2 <= dimension <= 7680 and dimension % 2 == 0 for dimension in (width, height)):
                return width, height
    raise VPError(
        "Resolution must be 720p, 1080p, 2160p, or WIDTHxHEIGHT with even dimensions from 2 to 7680"
    )


def canvas_filter(width: int, height: int) -> str:
    """Fit an image into a black canvas without cropping, using even pixel sizes.

    Pixel rounding can differ by less than two pixels from the exact aspect
    ratio. The two-pixel floor also makes extremely narrow canvases encodable.
    """
    if type(width) is not int or type(height) is not int:
        raise VPError("Canvas dimensions must be integers")
    parse_resolution(f"{width}x{height}")
    ratio = f"min({width}/(iw*sar),{height}/ih)"
    return (
        f"scale=w='max(2,trunc(iw*sar*{ratio}/2)*2)':h='max(2,trunc(ih*{ratio}/2)*2)',"
        f"setsar=1,pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=black"
    )


def export_video(
    job_dir: Path,
    job: dict,
    pdf: Path | None = None,
    soffice: Path | None = None,
    allow_draft: bool = False,
    include_hidden: bool = False,
    fps: int = 25,
    font_dirs: list[Path] | None = None,
    resolution: str = "1080p",
    duck_db: float = -18,
) -> dict:
    """Compose PDF slide backgrounds with optional timed local embedded video."""
    from .embedded_video import render_segment, static_samples, validate_duck_db, validate_inventory

    duck_db = validate_duck_db(duck_db)
    if type(fps) is not int or not 1 <= fps <= 60:
        raise VPError("fps must be an integer between 1 and 60")
    width, height = parse_resolution(resolution)
    slides = job.get("slides", [])
    if not slides:
        raise VPError("Presentation video export requires a PPTX slide inventory")
    selected = [slide for slide in slides if include_hidden or not slide.get("hidden", False)]
    if not selected:
        raise VPError("No visible slides; explicitly include hidden slides to export this presentation")
    if pdf is None and soffice is None:
        raise VPError(
            "Provide --pdf with every slide (including hidden slides) in presentation order, or an explicit --soffice path to Codex bundled LibreOffice",
            code="environment",
        )
    if pdf is not None and soffice is not None:
        raise VPError("Choose either an existing PDF or the bundled soffice backend")
    if pdf is not None and font_dirs:
        raise VPError("Font directories apply only to bundled soffice conversion, not an existing PDF")
    pdftoppm = _binary("pdftoppm")
    ffmpeg, ffprobe = _binary("ffmpeg"), _binary("ffprobe")
    directory = Path(job_dir).resolve()
    source = _inside(directory, job["source"]["copy"]) if job.get("source") else None
    if source and (not source.is_file() or sha256(source) != job.get("source_revision")):
        raise VPError("Immutable source changed", code="needs_recovery")
    if source and source.suffix.lower() == ".pptx":
        validate_inventory(source, job)
    elif any(slide.get("videos") or slide.get("timeline") for slide in slides):
        raise VPError(
            "Embedded video export requires the immutable PPTX; re-analyze the source", "needs_recovery"
        )
    embedded = any(slide.get("videos") for slide in selected)
    data = {}
    if embedded:
        data, (rate, _, _), quality = _load_chunks(directory, job, allow_draft)
        assembly = {"slides": [], "quality": quality}
    else:
        assembly = assemble(directory, job, allow_draft)
    by_slide = {slide["slide_id"]: slide for slide in assembly["slides"]}
    output = _inside(directory, "video/presentation.mp4")
    output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with tempfile.TemporaryDirectory(prefix=".video-", dir=output.parent) as temp:
        temporary = Path(temp)
        input_pdf = (
            Path(pdf).expanduser().resolve()
            if pdf is not None
            else _pdf_from_pptx(directory, job, Path(soffice), temporary, font_dirs=font_dirs)
        )
        if not input_pdf.is_file():
            raise VPError("Slide PDF does not exist")
        pdf_hash = sha256(input_pdf)
        page_by_id = {slide["id"]: index for index, slide in enumerate(slides)}
        metadata = _pdf_metadata(input_pdf, page_by_id[selected[0]["id"]])
        records, audio_pieces, segment_paths = [], [], []
        total_frames, total_samples = 0, 0
        if metadata["encrypted"] or metadata["pages"] != len(slides):
            raise VPError(
                "PDF must be unencrypted and contain exactly one page per inventoried slide, including hidden slides"
            )
        if not embedded:
            rate = by_slide[selected[0]["id"]]["sample_rate"]
        for index, slide in enumerate(selected):
            details = None
            if not slide.get("videos"):
                if embedded:
                    samples = static_samples(slide, job, data, rate)
                else:
                    audio = by_slide[slide["id"]]
                    samples, _ = sf.read(_inside(directory, audio["path"]), dtype="float64", always_2d=True)
                frames = math.ceil(len(samples) * fps / rate)
                # Cumulative rounding avoids slide-boundary sample drift.
                padded_samples = round((total_frames + frames) * rate / fps) - total_samples
                if padded_samples < len(samples):
                    frames += 1
                    padded_samples = round((total_frames + frames) * rate / fps) - total_samples
                padding = padded_samples - len(samples)
                audio_pieces.append(np.concatenate([samples, np.zeros((padding, samples.shape[1]))]))
            page_number = page_by_id[slide["id"]]
            page_metadata = metadata if index == 0 else _pdf_metadata(input_pdf, page_number)
            # Bound both raster dimensions while retaining the page aspect ratio.
            # A portrait page must not create a very tall width-scaled image.
            fit_by_width = width / page_metadata["width"] <= height / page_metadata["height"]
            image = temporary / f"slide-{index:04d}.png"
            _run(
                [
                    pdftoppm,
                    "-f",
                    str(page_number + 1),
                    "-l",
                    str(page_number + 1),
                    "-singlefile",
                    "-cropbox",
                    "-scale-to-x",
                    str(width) if fit_by_width else "-1",
                    "-scale-to-y",
                    "-1" if fit_by_width else str(height),
                    "-png",
                    str(input_pdf),
                    str(image.with_suffix("")),
                ],
                timeout=120,
            )
            segment = temporary / f"segment-{index:04d}.mp4"
            if slide.get("videos"):
                samples, details = render_segment(
                    source,
                    slide,
                    job,
                    data,
                    rate,
                    fps,
                    image,
                    page_metadata,
                    width,
                    height,
                    temporary,
                    segment,
                    ffmpeg,
                    ffprobe,
                    duck_db,
                    frame_offset=total_frames,
                    sample_offset=total_samples,
                )
                frames, padded_samples, padding = details["frames"], len(samples), 0
                audio_pieces.append(samples)
            else:
                _run(
                    [
                        ffmpeg,
                        "-hide_banner",
                        "-loglevel",
                        "error",
                        "-nostdin",
                        "-y",
                        "-loop",
                        "1",
                        "-framerate",
                        str(fps),
                        "-i",
                        str(image),
                        "-vf",
                        canvas_filter(width, height),
                        "-frames:v",
                        str(frames),
                        "-an",
                        "-c:v",
                        "libx264",
                        "-bf",
                        "0",
                        "-threads",
                        "1",
                        "-pix_fmt",
                        "yuv420p",
                        "-video_track_timescale",
                        str(fps * 1000),
                        str(segment),
                    ]
                )
            segment_paths.append(segment.name)
            records.append(
                {
                    "slide_id": slide["id"],
                    "source_order": slide["order"],
                    "pdf_page": page_number + 1,
                    "hidden": slide.get("hidden", False),
                    "start_frame": total_frames,
                    "end_frame": total_frames + frames,
                    "frames": frames,
                    "start_sample": total_samples,
                    "end_sample": total_samples + padded_samples,
                    "source_audio_frames": len(samples),
                    "added_padding_samples": padding,
                    "start_seconds": total_frames / fps,
                    "duration": frames / fps,
                }
            )
            if details is not None:
                records[-1]["embedded_video"] = details
            total_frames += frames
            total_samples += padded_samples
        concat = temporary / "segments.txt"
        concat.write_text("".join(f"file '{name}'\n" for name in segment_paths), encoding="utf-8")
        joined_audio = temporary / "video-audio.wav"
        sf.write(
            joined_audio,
            np.concatenate(audio_pieces),
            rate,
            subtype="FLOAT" if embedded else by_slide[selected[0]["id"]]["subtype"],
        )
        candidate = temporary / "presentation.mp4"
        _run(
            [
                ffmpeg,
                "-hide_banner",
                "-loglevel",
                "error",
                "-nostdin",
                "-y",
                "-f",
                "concat",
                "-safe",
                "1",
                "-i",
                str(concat),
                "-i",
                str(joined_audio),
                "-map",
                "0:v:0",
                "-map",
                "1:a:0",
                "-c:v",
                "copy",
                "-c:a",
                "aac",
                "-b:a",
                "192k",
                "-t",
                str(total_frames / fps),
                "-movflags",
                "+faststart",
                str(candidate),
            ],
            cwd=temporary,
        )
        probed = json.loads(
            _run(
                [
                    ffprobe,
                    "-v",
                    "error",
                    "-count_frames",
                    "-show_streams",
                    "-show_format",
                    "-of",
                    "json",
                    str(candidate),
                ]
            ).stdout
        )
        videos = [stream for stream in probed["streams"] if stream.get("codec_type") == "video"]
        audios = [stream for stream in probed["streams"] if stream.get("codec_type") == "audio"]
        if len(videos) != 1 or len(audios) != 1 or int(videos[0].get("nb_read_frames", -1)) != total_frames:
            raise VPError(
                "Exported video streams or frame count do not match the plan", code="needs_recovery"
            )
        if (int(videos[0].get("width", -1)), int(videos[0].get("height", -1))) != (width, height):
            raise VPError(
                "Exported video dimensions do not match the requested canvas", code="needs_recovery"
            )
        video_duration, audio_duration = float(videos[0]["duration"]), float(audios[0]["duration"])
        if (
            max(abs(video_duration - audio_duration), abs(video_duration - total_frames / fps))
            > 1 / fps + 1e-6
        ):
            raise VPError(
                "Exported audio/video duration differs by more than one frame", code="needs_recovery"
            )
        if sha256(input_pdf) != pdf_hash or (source and sha256(source) != job["source_revision"]):
            raise VPError("Source changed during video generation", code="needs_recovery")
        os.replace(candidate, output)
        # Representative output frames are evidence for user review, not a
        # substitute for matching the rendered page against PowerPoint.
        previews = []
        for index, record in enumerate(records):
            preview = output.parent / f"slide-{record['slide_id']}-preview.png"
            _run(
                [
                    ffmpeg,
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-nostdin",
                    "-y",
                    "-ss",
                    str(record["start_seconds"] + 0.5 / fps),
                    "-i",
                    str(output),
                    "-frames:v",
                    "1",
                    str(preview),
                ]
            )
            previews.append(
                {
                    "slide_id": record["slide_id"],
                    "path": str(preview.relative_to(directory)),
                    "sha256": sha256(preview),
                }
            )
    result = {
        "video": {
            "path": str(output.relative_to(directory)),
            "sha256": sha256(output),
            "fps": fps,
            "frames": total_frames,
            "duration": video_duration,
            "audio_duration": audio_duration,
            "audio_video_difference": abs(video_duration - audio_duration),
            "sample_rate": rate,
            "width": width,
            "height": height,
            "resolution": f"{width}x{height}",
            "channels": int(audios[0].get("channels", 0)),
        },
        "slides": records,
        "previews": previews,
        "quality": "draft",
        "audio_quality": assembly["quality"],
        "visual_review": "required",
        "pdf_sha256": pdf_hash,
        "backend": "provided_pdf" if pdf is not None else "bundled_soffice",
        "font_directories": [str(Path(value).expanduser().resolve()) for value in (font_dirs or [])],
        "warnings": (["embedded_video_composited"] if embedded else ["static_slides_only"])
        + ["visual_fidelity_requires_review"]
        + sorted({warning for slide in selected for warning in slide.get("warnings", [])}),
    }
    atomic_json(output.parent / "timeline.json", result)
    result["metadata_path"] = "video/timeline.json"
    return result
