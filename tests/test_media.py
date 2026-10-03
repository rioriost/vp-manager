import json
import os
import shutil
import xml.etree.ElementTree as ET
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf

from vp_manager.common import VPError, fingerprint, sha256
from vp_manager.media import BUNDLED_SOFFICE, _font_environment, _pdf_from_pptx, assemble, export_video


def make_job(directory, *, pptx=False, status="pass"):
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "renders").mkdir()
    chunks, renders, qa = [], {}, {}
    source_samples = []
    for index, (unit_id, length) in enumerate((("u0001", 1000), ("u0001", 1200), ("u0002", 1400))):
        chunk_id = f"c{index + 1:04d}"
        samples = ((np.arange(length) % 200 - 100) * 100).astype(np.int16)
        path = directory / "renders" / f"{chunk_id}.wav"
        sf.write(path, samples, 48000, subtype="PCM_16")
        chunks.append(
            {"id": chunk_id, "unit_id": unit_id, "slide_id": "256" if pptx else None, "text": "カナ"}
        )
        renders[chunk_id] = {
            "path": f"renders/{chunk_id}.wav",
            "sha256": sha256(path),
            "render_key": str(index),
        }
        qa[chunk_id] = {
            "status": status,
            "evidence_key": fingerprint({"audio": sha256(path), "expected": "カナ"}),
        }
        source_samples.append(sf.read(path, dtype="float64", always_2d=True)[0])
    slides = (
        [
            {"id": "256", "order": 1, "hidden": False, "warnings": []},
            {"id": "257", "order": 2, "hidden": True, "warnings": ["embedded_media_not_preserved"]},
            {"id": "258", "order": 3, "hidden": False, "warnings": []},
        ]
        if pptx
        else []
    )
    job = {
        "chunks": chunks,
        "renders": renders,
        "qa": qa,
        "slides": slides,
        "options": {"paragraph_pause": 0.01, "slide_lead": 0.02, "slide_tail": 0.03, "blank_duration": 0.12},
    }
    return job, source_samples


def test_assembly_preserves_original_samples_and_only_inserts_paragraph_pause(tmp_path):
    job, original = make_job(tmp_path)
    result = assemble(tmp_path, job)
    output, rate = sf.read(tmp_path / result["audio"]["path"], dtype="float64", always_2d=True)
    expected = np.concatenate([original[0], original[1], np.zeros((480, 1)), original[2]])
    assert rate == 48000
    assert np.array_equal(output, expected)
    assert result["quality"] == "verified"
    assert result["audio"]["subtype"] == "PCM_24"
    assert result["audio"]["chunks"][1]["start_sample"] == 1000
    assert result["audio"]["chunks"][2]["start_sample"] == 2680
    again = assemble(tmp_path, job)
    assert again["audio"]["sha256"] == result["audio"]["sha256"]


def test_slide_assembly_keeps_hidden_and_blank_slides(tmp_path):
    job, original = make_job(tmp_path, pptx=True)
    result = assemble(tmp_path, job)
    assert [value["slide_id"] for value in result["slides"]] == ["256", "257", "258"]
    assert result["slides"][1]["frames"] == 5760
    assert result["slides"][1]["empty_notes"]
    narrated, _ = sf.read(tmp_path / result["slides"][0]["path"], always_2d=True, dtype="float64")
    assert np.array_equal(narrated[960:1960], original[0])
    assert np.count_nonzero(narrated[:960]) == 0
    assert np.count_nonzero(narrated[-1440:]) == 0
    assert result["audio"]["frames"] == result["slides"][0]["frames"] + 5760
    assert [value["slide_id"] for value in result["audio"]["slides"]] == ["256", "258"]


def test_all_hidden_slide_inventory_still_has_individual_audio(tmp_path):
    job, _ = make_job(tmp_path, pptx=True)
    for slide in job["slides"]:
        slide["hidden"] = True
    result = assemble(tmp_path, job)
    assert result["audio"] is None
    assert len(result["slides"]) == 3
    assert "no_visible_slides" in result["warnings"]


@pytest.mark.parametrize("problem", ["missing", "hash", "fail", "uncertain", "stale", "format"])
def test_assembly_rejects_missing_changed_unreviewed_or_incompatible_audio(tmp_path, problem):
    job, _ = make_job(tmp_path)
    if problem == "missing":
        del job["renders"]["c0001"]
    elif problem == "hash":
        job["renders"]["c0001"]["sha256"] = "wrong"
    elif problem in {"fail", "uncertain"}:
        job["qa"]["c0001"]["status"] = problem
    elif problem == "stale":
        job["chunks"][0]["text"] = "カワッタ"
    elif problem == "format":
        path = tmp_path / job["renders"]["c0001"]["path"]
        sf.write(path, np.ones(100) * 0.1, 44100, subtype="PCM_16")
        job["renders"]["c0001"]["sha256"] = sha256(path)
        job["qa"]["c0001"] = {"status": "pass"}
    with pytest.raises(VPError):
        assemble(tmp_path, job)


def test_allow_draft_never_overrides_failed_qa(tmp_path):
    job, _ = make_job(tmp_path, status="uncertain")
    assert assemble(tmp_path, job, allow_draft=True)["quality"] == "draft"
    job["qa"]["c0001"]["status"] = "fail"
    with pytest.raises(VPError):
        assemble(tmp_path, job, allow_draft=True)


def write_pdf(path, count=3):
    fitz = pytest.importorskip("pymupdf")
    document = fitz.open()
    for index, color in enumerate([(1, 0, 0), (0, 1, 0), (0, 0, 1)][:count]):
        page = document.new_page(width=320, height=180)
        page.draw_rect(page.rect, color=color, fill=color)
        page.insert_text((20, 90), f"Slide {index + 1}", fontsize=24, color=(1, 1, 1))
    document.save(path)
    document.close()


@pytest.mark.skipif(
    not shutil.which("ffmpeg") or not shutil.which("ffprobe"),
    reason="FFmpeg integration dependencies unavailable",
)
@pytest.mark.parametrize("fps,include_hidden", [(25, False), (29, True)])
def test_video_static_frame_order_timing_and_draft_visual_status(tmp_path, fps, include_hidden):
    fitz = pytest.importorskip("pymupdf")
    directory = tmp_path / "job with spaces"
    job, _ = make_job(directory, pptx=True)
    pdf = tmp_path / "slides with spaces.pdf"
    write_pdf(pdf)
    before = sha256(pdf)
    result = export_video(directory, job, pdf=pdf, fps=fps, include_hidden=include_hidden)
    assert sha256(pdf) == before
    assert result["quality"] == "draft"
    assert result["audio_quality"] == "verified"
    assert result["visual_review"] == "required"
    expected = ["256", "257", "258"] if include_hidden else ["256", "258"]
    assert [record["slide_id"] for record in result["slides"]] == expected
    assert result["video"]["audio_video_difference"] <= 1 / fps
    assert sum(record["frames"] for record in result["slides"]) == result["video"]["frames"]
    for record in result["slides"]:
        assert record["added_padding_samples"] >= 0
        assert abs(record["start_sample"] / 48000 - record["start_frame"] / fps) <= 1 / 48000
        assert (
            record["end_sample"] - record["start_sample"]
            == record["source_audio_frames"] + record["added_padding_samples"]
        )
    expected_channels = [0, 1, 2] if include_hidden else [0, 2]
    for record, channel in zip(result["previews"], expected_channels, strict=True):
        pixmap = fitz.Pixmap(str(directory / record["path"]))
        pixels = np.frombuffer(pixmap.samples, dtype=np.uint8).reshape(-1, pixmap.n)
        assert np.argmax(pixels[:, :3].mean(axis=0)) == channel
    metadata = json.loads((directory / "video/timeline.json").read_text())
    assert metadata["video"]["sha256"] == sha256(directory / result["video"]["path"])


def test_video_requires_pdf_including_hidden_pages_and_explicit_backend(tmp_path):
    job, _ = make_job(tmp_path, pptx=True)
    with pytest.raises(VPError, match="Provide --pdf"):
        export_video(tmp_path, job)
    pdf = tmp_path / "missing-hidden.pdf"
    write_pdf(pdf, count=2)
    with pytest.raises(VPError, match="including hidden"):
        export_video(tmp_path, job, pdf=pdf)


def test_pdf_conversion_uses_bundled_backend_and_private_profile_only(tmp_path, monkeypatch):
    source = tmp_path / "source.pptx"
    source.write_bytes(b"synthetic mocked conversion source")
    job = {"source": {"copy": source.name}, "source_revision": sha256(source)}
    temporary = tmp_path / "convert"
    temporary.mkdir()
    before = sha256(source)
    with pytest.raises(VPError, match="bundled"):
        _pdf_from_pptx(tmp_path, job, Path("/Applications/LibreOffice.app/Contents/MacOS/soffice"), temporary)
    calls = []

    def fake_run(arguments, **kwargs):
        calls.append((arguments, kwargs))
        (temporary / "pdf/source.pdf").write_bytes(b"fake PDF only for command contract")

    monkeypatch.setattr("vp_manager.media._run", fake_run)
    fonts = tmp_path / "installed fonts"
    fonts.mkdir()
    output = _pdf_from_pptx(tmp_path, job, BUNDLED_SOFFICE, temporary, font_dirs=[fonts])
    assert output.is_file()
    assert sha256(source) == before
    arguments, options = calls[0]
    assert arguments[0] == str(BUNDLED_SOFFICE)
    assert any(value.startswith("-env:UserInstallation=file:") for value in arguments)
    assert "--headless" in arguments
    assert (
        '"ExportHiddenSlides":{"type":"boolean","value":"true"}'
        in arguments[arguments.index("--convert-to") + 1]
    )
    assert options["timeout"] == 180
    assert options["env"]["FONTCONFIG_FILE"] == str(temporary / "fonts.conf")


def test_font_configuration_is_subprocess_local_and_never_copies_fonts(tmp_path):
    fonts = tmp_path / "installed & licensed fonts"
    fonts.mkdir()
    (fonts / "font.ttf").write_bytes(b"do not modify or copy")
    temporary = tmp_path / "private-render"
    temporary.mkdir()
    before = dict(os.environ)
    environment = _font_environment(temporary, [fonts, fonts])
    assert dict(os.environ) == before
    assert environment["FONTCONFIG_PATH"] == str(temporary)
    xml = ET.parse(environment["FONTCONFIG_FILE"])
    assert [node.text for node in xml.findall("dir")] == [str(fonts)]
    assert xml.find("cachedir").text == str(temporary / "font-cache")
    assert not list(temporary.rglob("*.ttf"))
    assert (fonts / "font.ttf").read_bytes() == b"do not modify or copy"
    with pytest.raises(VPError, match="Font directory does not exist"):
        _font_environment(temporary, [tmp_path / "missing"])
