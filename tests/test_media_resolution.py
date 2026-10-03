"""Canvas validation and real Poppler/FFmpeg geometry tests; no speech engine."""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import numpy as np
import pytest

from vp_manager import media
from vp_manager.common import VPError, sha256
from vp_manager.media import canvas_filter, export_video, parse_resolution


@pytest.mark.parametrize("value,expected", [
    ("720p", (1280, 720)), ("1080p", (1920, 1080)), ("2160p", (3840, 2160)),
    ("720x1280", (720, 1280)), (" 640X480 ", (640, 480)), ("2x7680", (2, 7680)),
])
def test_resolution_presets_and_bounded_even_custom_canvas(value, expected):
    assert parse_resolution(value) == expected
    assert parse_resolution() == (1920, 1080)


@pytest.mark.parametrize("value", [
    "4k", "1921x1080", "1920x1079", "0x0", "1x2", "7682x1080", "1080x99999",
    "-2x100", "2.0x100", "1920:1080", "1920x1080;null", "", None, 1080, True,
])
def test_bad_resolution_rejected_before_creating_artifacts(tmp_path, value):
    with pytest.raises(VPError, match="Resolution"):
        export_video(tmp_path, {}, resolution=value)
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("width,height", [(True, 100), (100.0, 100), (101, 100), (100, 0)])
def test_canvas_helper_rejects_invalid_dimensions(width, height):
    with pytest.raises(VPError):
        canvas_filter(width, height)


TOOLS = all(shutil.which(name) for name in ("pdfinfo", "pdftoppm", "ffmpeg", "ffprobe"))
requires_tools = pytest.mark.skipif(not TOOLS, reason="Poppler and FFmpeg required for canvas integration")


def mixed_pages(tmp_path):
    fitz = pytest.importorskip("pymupdf")
    pdf = tmp_path / "mixed page sizes.pdf"
    document = fitz.open()
    for width, height, color in ((400, 300, (1, 0, 0)), (300, 600, (0, 1, 0))):
        page = document.new_page(width=width, height=height)
        page.draw_rect(page.rect, color=color, fill=color)
    document.save(pdf)
    document.close()
    job = {
        "slides": [{"id": str(256 + i), "order": i + 1, "hidden": False} for i in range(2)],
        "chunks": [], "renders": {}, "qa": {}, "options": {"blank_duration": 0.08},
    }
    return pdf, job


def pixels(path: Path):
    fitz = pytest.importorskip("pymupdf")
    image = fitz.Pixmap(str(path))
    return np.frombuffer(image.samples, dtype=np.uint8).reshape(image.height, image.width, image.n)[:, :, :3]


def assert_fitted_box(rgb, aspect, channel):
    height, width = rgb.shape[:2]
    foreground = rgb[:, :, channel] > 80
    ys, xs = np.nonzero(foreground)
    assert xs.size
    box = (int(xs.max() - xs.min() + 1), int(ys.max() - ys.min() + 1))
    factor = min(width / aspect, height)
    expected = (factor * aspect, factor)
    assert abs(box[0] - expected[0]) <= 4
    assert abs(box[1] - expected[1]) <= 4
    assert abs((xs.min() + xs.max() + 1) / 2 - width / 2) <= 2
    assert abs((ys.min() + ys.max() + 1) / 2 - height / 2) <= 2
    assert np.argmax(rgb[height // 2, width // 2]) == channel
    # These fixtures always have letterbox or pillarbox regions.
    assert rgb[0, 0].max() <= 5


@requires_tools
@pytest.mark.parametrize("resolution,dimensions", [
    (None, (1920, 1080)), ("720p", (1280, 720)), ("2160p", (3840, 2160)),
    ("720x1280", (720, 1280)),
])
def test_real_mixed_ratio_pages_fit_requested_canvas_without_distortion(tmp_path, resolution, dimensions):
    pdf, job = mixed_pages(tmp_path)
    original = sha256(pdf)
    kwargs = {} if resolution is None else {"resolution": resolution}
    result = export_video(tmp_path, job, pdf=pdf, fps=29, **kwargs)
    assert sha256(pdf) == original
    assert (result["video"]["width"], result["video"]["height"]) == dimensions
    assert result["video"]["resolution"] == f"{dimensions[0]}x{dimensions[1]}"
    assert result["video"]["audio_video_difference"] <= 1 / 29
    assert sum(record["frames"] for record in result["slides"]) == result["video"]["frames"]
    for preview, aspect, channel in zip(result["previews"], (4 / 3, 1 / 2), (0, 1), strict=True):
        rgb = pixels(tmp_path / preview["path"])
        assert rgb.shape[:2] == (dimensions[1], dimensions[0])
        assert_fitted_box(rgb, aspect, channel)
    metadata = json.loads((tmp_path / result["metadata_path"]).read_text())
    assert metadata["video"]["width"] == dimensions[0]
    assert result["quality"] == "draft"
    assert result["visual_review"] == "required"


@requires_tools
def test_probe_dimension_mismatch_does_not_replace_previous_video(tmp_path, monkeypatch):
    pdf, job = mixed_pages(tmp_path)
    output = tmp_path / "video/presentation.mp4"
    output.parent.mkdir()
    output.write_bytes(b"previous video to preserve")
    real_run = media._run

    def altered_probe(arguments, **kwargs):
        result = real_run(arguments, **kwargs)
        if Path(arguments[0]).name == "ffprobe":
            metadata = json.loads(result.stdout)
            next(stream for stream in metadata["streams"] if stream["codec_type"] == "video")["width"] = 2
            result.stdout = json.dumps(metadata)
        return result

    monkeypatch.setattr(media, "_run", altered_probe)
    with pytest.raises(VPError, match="dimensions") as caught:
        export_video(tmp_path, job, pdf=pdf, resolution="320x180")
    assert caught.value.code == "needs_recovery"
    assert output.read_bytes() == b"previous video to preserve"


@requires_tools
def test_reusable_filter_honors_nonsquare_source_pixels(tmp_path):
    output = tmp_path / "non-square-pixels.png"
    subprocess.run([
        shutil.which("ffmpeg"), "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
        "-f", "lavfi", "-i", "color=c=red:s=100x100", "-vf", "setsar=2," + canvas_filter(200, 200),
        "-frames:v", "1", str(output),
    ], check=True, capture_output=True, timeout=60)
    assert_fitted_box(pixels(output), 2, 0)
