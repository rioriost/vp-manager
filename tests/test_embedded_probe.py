"""Metadata and geometry contracts with mocked FFprobe output; no media process."""

import json
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest

from vp_manager import media
from vp_manager.common import VPError
from vp_manager.embedded_video import probe_clip, video_layout


@pytest.fixture
def metadata():
    return {
        "streams": [
            {
                "codec_type": "video",
                "width": 1920,
                "height": 1080,
                "sample_aspect_ratio": "1:1",
                "display_aspect_ratio": "16:9",
                "start_time": "0.000000",
                "duration": "12.5",
            },
            {"codec_type": "audio", "channels": 2, "start_time": "0.000000", "duration": "12.5"},
        ],
        "format": {"start_time": "0.000000", "duration": "12.5"},
    }


def run_probe(monkeypatch, metadata):
    calls = []

    def fake_run(arguments, **kwargs):
        calls.append(arguments)
        return SimpleNamespace(stdout=json.dumps(metadata))

    monkeypatch.setattr(media, "_run", fake_run)
    result = probe_clip(Path("/fixture with spaces/clip.mp4"), "/fake/ffprobe", 48000)
    assert len(calls) == 1
    assert calls[0][0] == "/fake/ffprobe"
    assert calls[0][-1] == "/fixture with spaces/clip.mp4"
    assert calls[0][calls[0].index("-protocol_whitelist") + 1] == "file,pipe"
    return result


@pytest.mark.parametrize(
    "sar,width,height,expected",
    [
        ("1:1", 1920, 1080, 16 / 9),
        ("8:9", 720, 480, 4 / 3),
        ("4:3", 720, 576, 5 / 3),
        ("N/A", 640, 480, 4 / 3),
        ("0:1", 640, 480, 4 / 3),
    ],
)
def test_probe_computes_display_aspect_from_pixel_dimensions_and_sar(
    monkeypatch, metadata, sar, width, height, expected
):
    metadata["streams"][0].update(width=width, height=height, sample_aspect_ratio=sar)
    result = run_probe(monkeypatch, metadata)
    assert result["display_aspect"] == pytest.approx(expected)
    assert result["duration"] == 12.5
    assert result["source_start_time"] == 0
    assert result["audio"]["channels"] == 2


@pytest.mark.parametrize("sar", ["1:0", "-1:1", "bad", "nan"])
def test_probe_rejects_invalid_sample_aspect(monkeypatch, metadata, sar):
    metadata["streams"][0]["sample_aspect_ratio"] = sar
    with pytest.raises(VPError):
        run_probe(monkeypatch, metadata)


@pytest.mark.parametrize(
    "location,rotation",
    [
        ("side", 90),
        ("side", -90),
        ("tag", "180"),
        ("side", "not-a-number"),
        ("tag", "90 degrees"),
        ("side", "NaN"),
        ("tag", "Infinity"),
    ],
)
def test_probe_rejects_rotated_or_malformed_rotation_metadata(monkeypatch, metadata, location, rotation):
    if location == "side":
        metadata["streams"][0]["side_data_list"] = [{"rotation": rotation}]
    else:
        metadata["streams"][0]["tags"] = {"rotate": rotation}
    with pytest.raises(VPError):
        run_probe(monkeypatch, metadata)


def test_full_turn_rotation_metadata_is_equivalent_to_unrotated(monkeypatch, metadata):
    metadata["streams"][0]["side_data_list"] = [{"rotation": 360}]
    metadata["streams"][0]["tags"] = {"rotate": "-360"}
    assert run_probe(monkeypatch, metadata)["display_aspect"] == pytest.approx(16 / 9)


@pytest.mark.parametrize("video_count,audio_count", [(0, 0), (0, 1), (2, 0), (1, 2)])
def test_probe_rejects_ambiguous_stream_counts(monkeypatch, metadata, video_count, audio_count):
    video, audio = metadata["streams"]
    metadata["streams"] = [deepcopy(video) for _ in range(video_count)] + [
        deepcopy(audio) for _ in range(audio_count)
    ]
    with pytest.raises(VPError, match="exactly one video"):
        run_probe(monkeypatch, metadata)


@pytest.mark.parametrize("channels", [0, 3, 6, "bad"])
def test_probe_rejects_unsupported_audio_channels(monkeypatch, metadata, channels):
    metadata["streams"][1]["channels"] = channels
    with pytest.raises(VPError):
        run_probe(monkeypatch, metadata)


@pytest.mark.parametrize("channels", [1, 2])
def test_probe_supports_mono_stereo_and_common_nonzero_stream_origin(monkeypatch, metadata, channels):
    for stream in metadata["streams"]:
        stream["start_time"] = "2.500000"
    metadata["streams"][1]["channels"] = channels
    result = run_probe(monkeypatch, metadata)
    assert result["source_start_time"] == 2.5
    assert result["duration"] == 12.5


def test_probe_supports_silent_video_and_format_duration_fallback(monkeypatch, metadata):
    metadata["streams"] = metadata["streams"][:1]
    del metadata["streams"][0]["duration"]
    assert run_probe(monkeypatch, metadata)["audio"] is None


@pytest.mark.parametrize("offset", [0.01, -0.01, "NaN", "Infinity"])
def test_probe_rejects_nonfinite_or_different_audio_video_origins(monkeypatch, metadata, offset):
    metadata["streams"][1]["start_time"] = str(offset)
    with pytest.raises(VPError):
        run_probe(monkeypatch, metadata)


def test_probe_allows_origin_difference_within_one_output_sample(monkeypatch, metadata):
    metadata["streams"][1]["start_time"] = str(0.5 / 48000)
    assert run_probe(monkeypatch, metadata)["source_start_time"] == 0


@pytest.mark.parametrize(
    "stream_index,duration",
    [
        (0, "0"),
        (0, "-1"),
        (0, "NaN"),
        (0, "Infinity"),
        (0, "14401"),
        (0, "bad"),
        (1, "0"),
        (1, "-1"),
        (1, "NaN"),
        (1, "Infinity"),
        (1, "14401"),
        (1, "bad"),
    ],
)
def test_each_stream_duration_must_be_finite_positive_and_bounded(
    monkeypatch, metadata, stream_index, duration
):
    metadata["streams"][stream_index]["duration"] = duration
    with pytest.raises(VPError):
        run_probe(monkeypatch, metadata)


def rectangle(x=0, y=0, width=3200000, height=1800000):
    return {"x_emu": x, "y_emu": y, "width_emu": width, "height_emu": height}


def test_exact_1080p_canvas_does_not_shrink_to_1918_pixels():
    result = video_layout(
        rectangle(), {"width_emu": 3200000, "height_emu": 1800000}, {"width": 960, "height": 540}, 1920, 1080
    )
    assert result == {
        "x": 0,
        "y": 0,
        "width": 1920,
        "height": 1080,
        "slide_canvas": {"width": 1920, "height": 1080, "x": 0, "y": 0},
    }


def test_four_three_slide_is_centered_with_horizontal_letterbox():
    result = video_layout(
        rectangle(width=4000, height=3000),
        {"width_emu": 4000, "height_emu": 3000},
        {"width": 800, "height": 600},
        1920,
        1080,
    )
    assert (result["x"], result["y"], result["width"], result["height"]) == (240, 0, 1440, 1080)
    assert result["slide_canvas"] == {"width": 1440, "height": 1080, "x": 240, "y": 0}


def test_landscape_slide_inside_portrait_output_keeps_geometry_and_even_pixels():
    result = video_layout(
        rectangle(x=1600000, y=900000, width=1600000, height=900000),
        {"width_emu": 3200000, "height_emu": 1800000},
        {"width": 960, "height": 540},
        1080,
        1920,
    )
    assert result["slide_canvas"] == {"width": 1080, "height": 606, "x": 0, "y": 657}
    assert (result["x"], result["y"], result["width"], result["height"]) == (540, 960, 540, 304)
    assert all(result[key] % 2 == 0 for key in ("x", "y", "width", "height"))


def test_portrait_slide_fits_exact_portrait_canvas():
    result = video_layout(
        rectangle(width=1800000, height=3200000),
        {"width_emu": 1800000, "height_emu": 3200000},
        {"width": 540, "height": 960},
        1080,
        1920,
    )
    assert (result["x"], result["y"], result["width"], result["height"]) == (0, 0, 1080, 1920)


@pytest.mark.parametrize(
    "video",
    [
        rectangle(x=-1),
        rectangle(y=-1),
        rectangle(x=1),
        rectangle(y=1),
        rectangle(width=0),
        rectangle(height=0),
        rectangle(width=True),
        rectangle(height="1800000"),
    ],
)
def test_off_slide_or_invalid_rectangles_are_rejected(video):
    with pytest.raises(VPError):
        video_layout(
            video, {"width_emu": 3200000, "height_emu": 1800000}, {"width": 960, "height": 540}, 1920, 1080
        )


def test_cropped_pdf_page_aspect_is_rejected():
    with pytest.raises(VPError, match="aspect/crop"):
        video_layout(
            rectangle(),
            {"width_emu": 3200000, "height_emu": 1800000},
            {"width": 800, "height": 600},
            1920,
            1080,
        )
