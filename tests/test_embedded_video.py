"""Sample scheduling, continuous duck envelopes, and local media-origin checks."""

import shutil
import subprocess

import numpy as np
import pytest

from vp_manager.common import VPError, sha256
from vp_manager.embedded_video import mix_audio, plan_timeline
from vp_manager.media import export_video
from vp_manager.pptx import read_pptx
from vp_manager.timed_notes import parse_timed_notes


def narration(notes, lengths, *, rate=1000):
    source = [
        {"id": f"u{i}", "source_text": text, "slide_id": "256", "paragraph_index": i}
        for i, text in enumerate(notes)
    ]
    parsed = parse_timed_notes(source)
    spoken = [unit for unit in parsed["units"] if not unit["note_control"] and unit["source_text"].strip()]
    chunks = [{"id": f"c{i}", "unit_id": unit["id"], "slide_id": "256"} for i, unit in enumerate(spoken)]
    data = {
        chunk["id"]: np.ones((round(length * rate), 1)) * 0.1
        for chunk, length in zip(chunks, lengths, strict=True)
    }
    return {"id": "256", "timeline": parsed["timeline"]}, parsed["units"], chunks, data


def test_soft_line_breaks_do_not_introduce_paragraph_pauses():
    args = narration(["【再生前】\n一つ。\n二つ。\n【動画を再生】"], [0.1, 0.1])
    plan = plan_timeline(*args, 1, 1000, 10, lead=0, tail=0, pause=0.4)
    assert plan["clip_start_sample"] == 200
    assert [item["start_sample"] for item in plan["narration_intervals"]] == [0, 100]
    separate = narration(["【再生前】", "一つ。", "二つ。", "【動画を再生】"], [0.1, 0.1])
    assert plan_timeline(*separate, 1, 1000, 10, lead=0, tail=0, pause=0.4)["clip_start_sample"] == 600


@pytest.mark.parametrize("offset", range(1, 15))
def test_frame_rounding_never_invents_lead_or_tail_at_29fps(offset):
    plan = plan_timeline(
        {"id": "256"},
        [],
        [],
        {},
        1,
        48000,
        29,
        lead=0,
        tail=0,
        frame_offset=offset,
        sample_offset=round(offset * 48000 / 29),
    )
    assert plan["clip_start_frame"] == 0
    assert plan["clip_end_frame"] == plan["frames"] == 29
    assert plan["samples"] == 48000


def test_cue_overlap_reports_measured_duration_and_missing_seconds():
    args = narration(["【動画を再生】", "【0:01付近】", "長い説明。", "【0:02付近】", "次。"], [1.2, 0.1])
    with pytest.raises(VPError, match=r"needs 1.200s.*only 1.000s.*short by 0.200s"):
        plan_timeline(*args, 3, 1000, 10)


def test_post_speech_follows_full_clip_and_cues_keep_original_offsets():
    args = narration(
        ["【再生前】", "冒頭。", "【動画を再生】", "【0:01付近】", "途中。", "【再生後】", "最後。"],
        [0.35, 0.2, 0.3],
    )
    plan = plan_timeline(*args, 2.03, 1000, 10, lead=0.2, tail=0.2)
    assert plan["clip_start_sample"] == 600
    assert plan["clip_end_sample"] == 2700
    assert [item["start_sample"] for item in plan["narration_intervals"]] == [200, 1600, 2700]
    assert plan["samples"] == 3200


def test_contiguous_chunks_share_one_duck_span_without_boundary_pumping():
    data = {"a": np.zeros((200, 1)), "b": np.zeros((200, 1))}
    plan = {
        "sample_rate": 1000,
        "samples": 1000,
        "clip_start_sample": 0,
        "clip_end_sample": 1000,
        "narration_intervals": [
            {"chunk_id": "a", "start_sample": 200, "end_sample": 400},
            {"chunk_id": "b", "start_sample": 400, "end_sample": 600},
        ],
    }
    mixed, report = mix_audio(plan, data, np.ones((1000, 2)) * 0.2, duck_db=-18)
    expected = 0.2 * 10 ** (-18 / 20)
    assert mixed[399, 0] == pytest.approx(expected)
    assert mixed[400, 0] == pytest.approx(expected)
    assert report["duck_intervals"] == [{"start_sample": 200, "end_sample": 600}]
    assert np.all(mixed[:200] == 0.2)
    assert np.all(mixed[600:] == 0.2)


def test_real_silent_gap_restores_original_audio_and_short_speech_ramps_fit():
    data = {"a": np.zeros((20, 1)), "b": np.zeros((20, 1))}
    plan = {
        "sample_rate": 1000,
        "samples": 200,
        "clip_start_sample": 0,
        "clip_end_sample": 200,
        "narration_intervals": [
            {"chunk_id": "a", "start_sample": 50, "end_sample": 70},
            {"chunk_id": "b", "start_sample": 100, "end_sample": 120},
        ],
    }
    mixed, _ = mix_audio(plan, data, np.ones((200, 1)) * 0.2, duck_db=-18)
    assert np.all(mixed[70:100] == 0.2)
    assert mixed[59, 0] == pytest.approx(0.2 * 10 ** (-18 / 20))
    assert np.array_equal(mixed[:, 0], mixed[:, 1])


def test_headroom_gain_is_applied_only_when_needed_and_reported():
    plan = {
        "sample_rate": 1000,
        "samples": 100,
        "clip_start_sample": 0,
        "clip_end_sample": 100,
        "narration_intervals": [{"chunk_id": "a", "start_sample": 0, "end_sample": 100}],
    }
    mixed, report = mix_audio(plan, {"a": np.ones((100, 1)) * 0.9}, np.ones((100, 2)) * 0.8, duck_db=0)
    assert report["peak_before_headroom"] == pytest.approx(1.7)
    assert report["headroom_gain"] == pytest.approx(0.98 / 1.7)
    assert np.max(mixed) == pytest.approx(0.98)
    _, quiet = mix_audio(plan, {"a": np.ones((100, 1)) * 0.1}, np.ones((100, 2)) * 0.2, duck_db=0)
    assert quiet["headroom_gain"] == 1


@pytest.mark.parametrize("duck_db", [True, None, float("nan"), float("inf"), -61, 1])
def test_bad_duck_values_rejected_before_media_work(tmp_path, duck_db):
    with pytest.raises(VPError, match="duck_db"):
        export_video(tmp_path, {}, duck_db=duck_db)
    assert not list(tmp_path.iterdir())


@pytest.mark.skipif(
    not all(shutil.which(name) for name in ("ffmpeg", "ffprobe", "pdfinfo", "pdftoppm")),
    reason="FFmpeg and Poppler required",
)
def test_actual_equal_nonzero_stream_origins_normalize_without_av_shift(tmp_path):
    from test_pptx_video import make_video_pptx

    fitz = pytest.importorskip("pymupdf")
    output_audio = []
    for origin in (0, 1):
        directory = tmp_path / str(origin)
        directory.mkdir()
        clip = directory / "offset.mov"
        subprocess.run(
            [
                "ffmpeg",
                "-v",
                "error",
                "-y",
                "-f",
                "lavfi",
                "-i",
                "color=c=red:s=160x90:r=10:d=1",
                "-f",
                "lavfi",
                "-i",
                "sine=frequency=440:sample_rate=48000:duration=1",
                "-map",
                "0:v:0",
                "-map",
                "1:a:0",
                "-c:v",
                "libx264",
                "-bf",
                "0",
                "-c:a",
                "pcm_s16le",
                "-output_ts_offset",
                str(origin),
                str(clip),
            ],
            check=True,
            capture_output=True,
            timeout=60,
        )
        source = make_video_pptx(
            directory / "source.pptx",
            notes=["【動画を再生】"],
            video_bytes=clip.read_bytes(),
            extension="mov",
            mime="video/quicktime",
        )
        job = {
            **read_pptx(source),
            "source": {"copy": "source.pptx"},
            "source_revision": sha256(source),
            "chunks": [],
            "renders": {},
            "qa": {},
            "options": {"slide_lead": 0.2, "slide_tail": 0.2},
        }
        pdf = directory / "slides.pdf"
        document = fitz.open()
        page = document.new_page(width=1000, height=600)
        page.draw_rect(page.rect, fill=(0, 1, 0))
        document.save(pdf)
        document.close()
        result = export_video(directory, job, pdf=pdf, fps=10, resolution="320x192")
        measured = result["slides"][0]["embedded_video"]
        assert measured["source_start_time"] == origin
        assert measured["clip_start_sample"] == 9600
        assert measured["clip_end_sample"] == 57600
        assert result["video"]["frames"] == 14
        decoded = subprocess.run(
            [
                "ffmpeg",
                "-v",
                "error",
                "-i",
                str(directory / result["video"]["path"]),
                "-vn",
                "-f",
                "f32le",
                "-ac",
                "2",
                "-ar",
                "48000",
                "pipe:1",
            ],
            capture_output=True,
            check=True,
            timeout=60,
        ).stdout
        output_audio.append(np.frombuffer(decoded, dtype="<f4"))
    assert np.array_equal(output_audio[0], output_audio[1])
