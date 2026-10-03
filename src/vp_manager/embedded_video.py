"""Measured narration scheduling and local embedded-video composition."""

from __future__ import annotations

import json
import math
from fractions import Fraction
from pathlib import Path

import numpy as np
import soundfile as sf

from .common import VPError, fingerprint, sha256
from .pptx import extract_video, read_pptx

_MEDIA_FORMATS = "mov,matroska,webm,avi,asf,mpeg,mpegts,ogg"


def validate_inventory(source: Path, job: dict) -> bool:
    """Never trust persisted media geometry or cue/source spans without reparsing."""
    fresh = read_pptx(source)
    relevant = any(s.get("videos") or s.get("timeline") for s in fresh["slides"] + job.get("slides", []))
    if not relevant:
        return False
    if fresh.get("presentation_size") != job.get("presentation_size"):
        raise VPError("Presentation dimensions changed; re-analyze the immutable PPTX", "needs_recovery")
    keys = ("id", "order", "part", "hidden", "unit_ids", "videos", "timeline")
    expected = [{key: slide.get(key) for key in keys} for slide in fresh["slides"]]
    observed = [{key: slide.get(key) for key in keys} for slide in job.get("slides", [])]
    if expected != observed:
        raise VPError(
            "Embedded media or timed-note inventory is stale; re-analyze the PPTX", "needs_recovery"
        )
    stored = {unit["id"]: unit for unit in job.get("units", [])}
    if len(stored) != len(job.get("units", [])) or set(stored) != {unit["id"] for unit in fresh["units"]}:
        raise VPError("Source unit inventory changed; re-analyze the PPTX", "needs_recovery")
    for unit in fresh["units"]:
        if any(stored[unit["id"]].get(key) != value for key, value in unit.items() if key != "spoken_text"):
            raise VPError("Source notes or timed controls changed; re-analyze the PPTX", "needs_recovery")
        if unit.get("note_control") and stored[unit["id"]].get("spoken_text", ""):
            raise VPError("Playback instructions cannot be spoken narration", "needs_recovery")
    for slide in fresh["slides"]:
        videos = slide.get("videos", [])
        if slide.get("timeline") and not videos:
            raise VPError(f"Slide {slide['id']}: timed notes require an embedded video", "needs_decision")
        if not videos:
            continue
        if len(videos) != 1 or videos[0].get("unsupported"):
            reasons = sorted({reason for video in videos for reason in video.get("unsupported", [])})
            raise VPError(
                f"Slide {slide['id']}: unsupported embedded video: {reasons or ['multiple_videos']}",
                "needs_decision",
            )
        if not slide.get("timeline") and any(
            unit["source_text"].strip() for unit in fresh["units"] if unit.get("slide_id") == slide["id"]
        ):
            raise VPError(
                f"Slide {slide['id']}: add standalone 【再生前】, 【動画を再生】, 【0:30付近】, "
                "and 【再生後】 markers to timed video notes",
                "needs_decision",
            )
    return True


def validate_duck_db(value: float) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (float, int))
        or not math.isfinite(value)
        or not -60 <= value <= 0
    ):
        raise VPError("duck_db must be a finite number from -60 to 0")
    return float(value)


def stereo(samples: np.ndarray) -> np.ndarray:
    if samples.ndim != 2 or samples.shape[1] not in (1, 2):
        raise VPError("Video narration supports mono or stereo audio")
    return np.repeat(samples, 2, axis=1) if samples.shape[1] == 1 else samples


def static_samples(slide: dict, job: dict, data: dict, rate: int) -> np.ndarray:
    """Build only this static segment in memory; never replace assembled artifacts."""
    from .media import _seconds

    options = job.get("options", {})
    pieces, previous = [], None
    for chunk in job.get("chunks", []):
        if chunk.get("slide_id") != slide["id"]:
            continue
        if previous is not None and previous != chunk["unit_id"]:
            pieces.append(np.zeros((round(_seconds(options, "paragraph_pause", 0.4) * rate), 2)))
        pieces.append(stereo(data[chunk["id"]]))
        previous = chunk["unit_id"]
    if not pieces:
        count = round(_seconds(options, "blank_duration", 3) * rate)
        if count < 1:
            raise VPError("Blank slide duration must contain at least one sample")
        return np.zeros((count, 2))
    return np.concatenate(
        [
            np.zeros((round(_seconds(options, "slide_lead", 0.25) * rate), 2)),
            *pieces,
            np.zeros((round(_seconds(options, "slide_tail", 0.5) * rate), 2)),
        ]
    )


def plan_timeline(
    slide: dict,
    units: list[dict],
    chunks: list[dict],
    data: dict,
    clip_duration: float,
    rate: int,
    fps: int,
    *,
    lead: float = 0.25,
    tail: float = 0.5,
    pause: float = 0.4,
    frame_offset: int = 0,
    sample_offset: int = 0,
) -> dict:
    """Measure all speech first; never shorten a clip or silently move its cues."""
    if not math.isfinite(clip_duration) or not 0 < clip_duration <= 14400:
        raise VPError("Embedded video duration must be positive and at most four hours")
    timeline = slide.get("timeline") or {"before_unit_ids": [], "cues": [], "after_unit_ids": []}
    by_unit = {unit["id"]: unit for unit in units}
    slide_chunks = [chunk for chunk in chunks if chunk.get("slide_id") == slide["id"]]
    groups: dict[str, list[dict]] = {}
    for chunk in slide_chunks:
        unit = by_unit.get(chunk["unit_id"])
        if unit is None or unit.get("note_control") or unit.get("slide_id") != slide["id"]:
            raise VPError("Narration chunk points to an unknown or control unit", "needs_recovery")
        groups.setdefault(chunk["unit_id"], []).append(chunk)
    intervals = []

    def frame_sample(frame):
        return round(Fraction((frame_offset + frame) * rate, fps)) - sample_offset

    def covering_frame(sample):
        # Find the first rounded sample boundary that covers the requested
        # sample. Sub-sample rounding at a previous slide must not add a frame.
        numerator = (2 * (sample_offset + sample) - 1) * fps
        frame = -(-numerator // (2 * rate)) - frame_offset
        if frame_sample(frame) < sample:
            frame += 1
        return frame

    def place(unit_ids, start, phase, cue_seconds=None):
        previous = None
        for unit_id in unit_ids:
            unit = by_unit.get(unit_id)
            if unit is None or unit.get("note_control") or unit.get("slide_id") != slide["id"]:
                raise VPError("Invalid narration unit in timed notes", "needs_recovery")
            pieces = groups.get(unit_id, [])
            if not pieces and unit.get("spoken_text", unit["source_text"]).strip():
                raise VPError(
                    f"Slide {slide['id']}: missing rendered narration for {unit_id}", "needs_decision"
                )
            paragraph = unit.get("source_unit_id", unit_id)
            if pieces and previous is not None and paragraph != previous:
                start += round(pause * rate)
            for chunk in pieces:
                length = len(data[chunk["id"]])
                intervals.append(
                    {
                        "chunk_id": chunk["id"],
                        "unit_id": unit_id,
                        "phase": phase,
                        "cue_seconds": cue_seconds,
                        "start_sample": start,
                        "end_sample": start + length,
                    }
                )
                start += length
            if pieces:
                previous = paragraph
        return start

    before_end = place(timeline.get("before_unit_ids", []), round(lead * rate), "before")
    clip_start_frame = covering_frame(before_end)
    clip_start = frame_sample(clip_start_frame)
    clip_frames = math.ceil(clip_duration * fps - 1e-9)
    clip_end_frame = clip_start_frame + clip_frames
    clip_end = frame_sample(clip_end_frame)
    if clip_end - clip_start < round(clip_duration * rate):
        clip_end_frame += 1
        clip_end = frame_sample(clip_end_frame)
    cues = timeline.get("cues", [])
    previous_seconds = -1.0
    for index, cue in enumerate(cues):
        seconds = cue.get("at_seconds")
        if (
            isinstance(seconds, bool)
            or not isinstance(seconds, (float, int))
            or not math.isfinite(seconds)
            or not previous_seconds < seconds < clip_duration
        ):
            raise VPError(
                f"Slide {slide['id']}: cue {seconds!r}s must increase and lie before "
                f"video end {clip_duration:.3f}s",
                "needs_decision",
            )
        previous_seconds = seconds
        start = clip_start + round(seconds * rate)
        end = place(cue.get("unit_ids", []), start, "video", seconds)
        limit_seconds = cues[index + 1]["at_seconds"] if index + 1 < len(cues) else clip_duration
        if not isinstance(limit_seconds, (float, int)) or not math.isfinite(limit_seconds):
            raise VPError("Invalid next cue time", "needs_decision")
        limit = clip_start + round(limit_seconds * rate)
        if end > limit:
            raise VPError(
                f"Slide {slide['id']}: cue at {seconds:.3f}s needs {(end - start) / rate:.3f}s "
                f"of narration, but only {(limit - start) / rate:.3f}s is available; "
                f"short by {(end - limit) / rate:.3f}s. Shorten narration or revise the cue.",
                "needs_decision",
            )
    after_end = place(timeline.get("after_unit_ids", []), clip_end, "after")
    planned = [item["chunk_id"] for item in intervals]
    if len(planned) != len(set(planned)) or set(planned) != {chunk["id"] for chunk in slide_chunks}:
        raise VPError("Timed narration is missing, repeated, or outside a playback phase", "needs_recovery")
    total_frames = covering_frame(after_end + round(tail * rate))
    return {
        "clip_duration": clip_duration,
        "clip_start_sample": clip_start,
        "clip_end_sample": clip_end,
        "clip_start_frame": clip_start_frame,
        "clip_end_frame": clip_end_frame,
        "frames": total_frames,
        "samples": frame_sample(total_frames),
        "sample_rate": rate,
        "narration_intervals": intervals,
        "quantization": "clip boundaries round up to frames; cues round to samples",
    }


def mix_audio(
    plan: dict, data: dict, original: np.ndarray, *, duck_db: float = -18
) -> tuple[np.ndarray, dict]:
    duck_db = validate_duck_db(duck_db)
    rate, count = plan["sample_rate"], plan["samples"]
    bed = np.zeros((count, 2), dtype=np.float64)
    start = plan["clip_start_sample"]
    if start + len(original) > plan["clip_end_sample"]:
        raise VPError("Decoded media audio exceeds the measured playback window", "needs_recovery")
    bed[start : start + len(original)] = stereo(original)
    speech = np.zeros_like(bed)
    envelope = np.ones(count, dtype=np.float64)
    gain = 10 ** (duck_db / 20)
    ramp = round(0.05 * rate)
    for interval in plan["narration_intervals"]:
        left, right = interval["start_sample"], interval["end_sample"]
        speech[left:right] += stereo(data[interval["chunk_id"]])
    occupied = []
    for interval in sorted(plan["narration_intervals"], key=lambda value: value["start_sample"]):
        left, right = interval["start_sample"], interval["end_sample"]
        if occupied and left <= occupied[-1][1]:
            occupied[-1][1] = max(right, occupied[-1][1])
        else:
            occupied.append([left, right])
    for left, right in occupied:
        local = np.full(right - left, gain, dtype=np.float64)
        edge = min(ramp, len(local) // 2)
        if edge:
            local[:edge] = np.linspace(1, gain, edge)
            local[-edge:] = np.linspace(gain, 1, edge)
        envelope[left:right] = np.minimum(envelope[left:right], local)
    mixed = bed * envelope[:, None] + speech
    peak = float(np.max(np.abs(mixed))) if len(mixed) else 0.0
    headroom = min(1.0, 0.98 / peak) if peak else 1.0
    mixed *= headroom
    if not np.isfinite(mixed).all():
        raise VPError("Nonfinite mixed audio", "needs_recovery")
    return mixed, {
        "duck_db": duck_db,
        "duck_ramp_samples": ramp,
        "duck_ramp_policy": "linear ramps inside each continuous speech span; unity outside speech",
        "duck_intervals": [{"start_sample": left, "end_sample": right} for left, right in occupied],
        "headroom_gain": headroom,
        "peak_before_headroom": peak,
        "peak_after_headroom": peak * headroom,
        "source_audio_peak": float(np.max(np.abs(original))) if len(original) else 0.0,
        "narration_peak": float(np.max(np.abs(speech))) if len(speech) else 0.0,
    }


def video_layout(video: dict, presentation: dict, page: dict, width: int, height: int) -> dict:
    try:
        sw, sh = presentation["width_emu"], presentation["height_emu"]
        x, y, vw, vh = (video[key] for key in ("x_emu", "y_emu", "width_emu", "height_emu"))
        if any(type(v) is not int for v in (sw, sh, x, y, vw, vh)) or min(sw, sh, vw, vh) <= 0:
            raise ValueError("dimensions")
        if min(x, y) < 0 or x + vw > sw or y + vh > sh:
            raise ValueError("off-slide rectangle")
    except (KeyError, TypeError, ValueError) as exc:
        raise VPError("Embedded video requires a valid on-slide rectangle", "needs_decision") from exc
    if abs((page["width"] / page["height"]) / (sw / sh) - 1) > 0.001:
        raise VPError(
            "PDF page aspect/crop does not match the PPTX slide; supply an uncropped all-slide PDF",
            "needs_decision",
        )
    factor = min(Fraction(width, sw), Fraction(height, sh))
    fitted_w, fitted_h = max(2, int(sw * factor // 2) * 2), max(2, int(sh * factor // 2) * 2)
    rect = {
        "x": round((Fraction(width - fitted_w, 2) + Fraction(x, sw) * fitted_w) / 2) * 2,
        "y": round((Fraction(height - fitted_h, 2) + Fraction(y, sh) * fitted_h) / 2) * 2,
        "width": round(Fraction(vw, sw) * fitted_w / 2) * 2,
        "height": round(Fraction(vh, sh) * fitted_h / 2) * 2,
    }
    if (
        min(rect["width"], rect["height"]) < 2
        or rect["x"] + rect["width"] > width
        or rect["y"] + rect["height"] > height
    ):
        raise VPError("Video rectangle cannot be represented on this resolution", "needs_decision")
    return {
        **rect,
        "slide_canvas": {
            "width": fitted_w,
            "height": fitted_h,
            "x": (width - fitted_w) / 2,
            "y": (height - fitted_h) / 2,
        },
    }


def probe_clip(path: Path, ffprobe: str, rate: int) -> dict:
    from .media import _run

    result = json.loads(
        _run(
            [
                ffprobe,
                "-v",
                "error",
                "-protocol_whitelist",
                "file,pipe",
                "-format_whitelist",
                _MEDIA_FORMATS,
                "-show_streams",
                "-show_format",
                "-of",
                "json",
                str(path),
            ]
        ).stdout
    )
    videos = [stream for stream in result.get("streams", []) if stream.get("codec_type") == "video"]
    audios = [stream for stream in result.get("streams", []) if stream.get("codec_type") == "audio"]
    if len(videos) != 1 or len(audios) > 1:
        raise VPError(
            "Embedded clip must have exactly one video and at most one audio stream", "needs_decision"
        )
    video = videos[0]
    try:
        if (
            any(float(item.get("rotation", 0)) % 360 for item in video.get("side_data_list", []))
            or float(video.get("tags", {}).get("rotate", 0)) % 360
        ):
            raise VPError("Rotated embedded video streams are unsupported", "needs_decision")
        sample_aspect = video.get("sample_aspect_ratio", "1:1")
        sar = (
            Fraction(sample_aspect.replace(":", "/")) if sample_aspect not in {"N/A", "0:1"} else Fraction(1)
        )
        display_aspect = int(video["width"]) * sar / int(video["height"])
        if display_aspect <= 0:
            raise ValueError("display aspect")
        origin = float(video.get("start_time", result.get("format", {}).get("start_time", 0)))
        duration = float(video.get("duration", result.get("format", {}).get("duration")))
        if not math.isfinite(origin) or not math.isfinite(duration) or not 0 < duration <= 14400:
            raise ValueError("video duration/start")
        if audios:
            if int(audios[0].get("channels", 0)) not in (1, 2):
                raise VPError("Embedded audio supports mono or stereo channels", "needs_decision")
            audio_start = float(audios[0].get("start_time", origin))
            if not math.isfinite(audio_start):
                raise ValueError("audio start")
            if abs(audio_start - origin) > 1 / rate:
                raise VPError(
                    "Embedded audio/video stream start offsets differ; normalize the clip first",
                    "needs_decision",
                )
            audio_duration = float(audios[0].get("duration", duration))
            if not math.isfinite(audio_duration) or not 0 < audio_duration <= 14400:
                raise ValueError("audio duration")
            duration = max(duration, audio_duration)
    except (KeyError, ValueError, TypeError, ZeroDivisionError) as exc:
        raise VPError("Embedded clip has no finite supported duration", "needs_decision") from exc
    return {
        "duration": duration,
        "source_start_time": origin,
        "display_aspect": float(display_aspect),
        "video": video,
        "audio": audios[0] if audios else None,
    }


def render_segment(
    source: Path,
    slide: dict,
    job: dict,
    data: dict,
    rate: int,
    fps: int,
    image: Path,
    page: dict,
    width: int,
    height: int,
    temporary: Path,
    segment: Path,
    ffmpeg: str,
    ffprobe: str,
    duck_db: float,
    frame_offset: int = 0,
    sample_offset: int = 0,
) -> tuple[np.ndarray, dict]:
    from .media import _run, _seconds, canvas_filter

    video = slide["videos"][0]
    clip = temporary / f"media-{slide['id']}.bin"
    extract_video(source, video, clip)
    original_hash = sha256(clip)
    probed = probe_clip(clip, ffprobe, rate)
    layout = video_layout(video, job.get("presentation_size"), page, width, height)
    if abs((video["width_emu"] / video["height_emu"]) / probed["display_aspect"] - 1) > 0.001:
        raise VPError(
            "Video placement aspect differs from the clip display aspect; stretched placements "
            "are unsupported. Restore the video's original aspect ratio in PowerPoint.",
            "needs_decision",
        )
    original = np.zeros((0, 2), dtype=np.float64)
    if probed["audio"]:
        decoded = temporary / f"original-{slide['id']}.wav"
        command = [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-nostdin",
            "-y",
            "-protocol_whitelist",
            "file,pipe",
            "-format_whitelist",
            _MEDIA_FORMATS,
            "-i",
            str(clip),
            "-map",
            "0:a:0",
            "-vn",
        ]
        if int(probed["audio"]["channels"]) == 1:
            command += ["-af", "pan=stereo|c0=c0|c1=c0"]
        _run(command + ["-ac", "2", "-ar", str(rate), "-c:a", "pcm_f32le", str(decoded)])
        original, _ = sf.read(decoded, dtype="float64", always_2d=True)
        probed["duration"] = max(probed["duration"], len(original) / rate)
    options = job.get("options", {})
    plan = plan_timeline(
        slide,
        job.get("units", []),
        job.get("chunks", []),
        data,
        probed["duration"],
        rate,
        fps,
        lead=_seconds(options, "slide_lead", 0.25),
        tail=_seconds(options, "slide_tail", 0.5),
        pause=_seconds(options, "paragraph_pause", 0.4),
        frame_offset=frame_offset,
        sample_offset=sample_offset,
    )
    samples, mix = mix_audio(plan, data, original, duck_db=duck_db)
    clip_start = plan["clip_start_frame"] / fps
    graph = (
        f"[0:v]{canvas_filter(width, height)}[base];"
        f"[1:v]setpts=PTS-STARTPTS,fps={fps},{canvas_filter(layout['width'], layout['height'])},"
        f"tpad=stop_mode=clone:stop_duration={plan['frames'] / fps},setpts=PTS+{clip_start}/TB[moving];"
        f"[base][moving]overlay=x={layout['x']}:y={layout['y']}:"
        f"enable='gte(t,{clip_start})':eof_action=repeat:repeatlast=1[out]"
    )
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
            "-protocol_whitelist",
            "file,pipe",
            "-format_whitelist",
            _MEDIA_FORMATS,
            "-noautorotate",
            "-i",
            str(clip),
            "-filter_complex_threads",
            "1",
            "-filter_complex",
            graph,
            "-map",
            "[out]",
            "-frames:v",
            str(plan["frames"]),
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
    provenance = {
        "schema_version": 1,
        "source_sha256": job["source_revision"],
        "media_sha256": original_hash,
        "layout": layout,
        "media_inventory": video,
        "timeline": slide.get("timeline"),
        "fps": fps,
        "width": width,
        "height": height,
        "duck_db": duck_db,
        "measured_plan": plan,
        "scheduling": {
            key: _seconds(options, key, default)
            for key, default in (("slide_lead", 0.25), ("slide_tail", 0.5), ("paragraph_pause", 0.4))
        },
        "narration_hashes": {
            item["chunk_id"]: job["renders"][item["chunk_id"]]["sha256"]
            for item in plan["narration_intervals"]
        },
    }
    return samples, {
        **plan,
        **mix,
        "layout": layout,
        "media_sha256": original_hash,
        "source_start_time": probed["source_start_time"],
        "has_original_audio": bool(probed["audio"]),
        "original_audio_samples": len(original),
        "playback_rate": 1.0,
        "audio_normalization": "original media resampled to narration rate; mono duplicated to stereo",
        "channels": 2,
        "provenance": provenance,
        "provenance_hash": fingerprint(provenance),
    }
