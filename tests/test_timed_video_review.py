"""Independent timeline review using disposable notes, WAVs, PDFs and video clips."""

import shutil
import subprocess
from copy import deepcopy
from xml.sax.saxutils import escape
from zipfile import ZipFile

import numpy as np
import pytest
import soundfile as sf

from vp_manager.common import VPError, sha256
from vp_manager.decisions import apply_decisions
from vp_manager.embedded_video import mix_audio, plan_timeline
from vp_manager.media import assemble, export_video
from vp_manager.pptx import read_pptx
from vp_manager.qa import evidence_key
from vp_manager.text import detect_candidates, plan_chunks
from vp_manager.timed_notes import parse_timed_notes


def note_units(texts):
    return [
        {"id": f"u{index:04d}", "source_text": text, "slide_id": "256", "paragraph_index": index}
        for index, text in enumerate(texts, 1)
    ]


def test_soft_break_controls_are_lossless_and_keep_original_paragraph_identity():
    original = note_units([
        "【再生前】\r\n  はじめに。\r\n\r\n【動画を再生】\n",
        "【0:30付近】\n途中です。\n【1:45付近】\n次です。",
        "【再生後】\nおわりです。",
    ])
    before = deepcopy(original)
    result = parse_timed_notes(original)
    assert original == before
    timeline = result["timeline"]
    assert [cue["at_seconds"] for cue in timeline["cues"]] == [30, 105]
    for source in original:
        children = [unit for unit in result["units"] if unit["source_unit_id"] == source["id"]]
        assert "".join(unit["source_text"] for unit in children) == source["source_text"]
        for unit in children:
            assert unit["source_text"] == source["source_text"][unit["source_start"]:unit["source_end"]]
            assert unit["paragraph_index"] == source["paragraph_index"]
    controls = [unit for unit in result["units"] if unit["note_control"]]
    assert len(controls) == 5
    assert all(unit["spoken_text"] == "" for unit in controls)
    chunks = plan_chunks(result["units"])
    assert [chunk["text"] for chunk in chunks] == ["  はじめに。", "途中です。", "次です。", "おわりです。"]


def test_reading_snapshot_cannot_restore_control_markers_to_speech():
    parsed = parse_timed_notes(note_units(["【動画を再生】\n【0:30付近】\nABCを確認。\n【再生後】"]))
    units = parsed["units"]
    candidates = detect_candidates(units, [])
    assert [candidate["surface"] for candidate in candidates] == ["ABC"]
    candidate = candidates[0]
    result = apply_decisions(units, candidates, "revision", {
        "schema_version": 1, "source_revision": "revision",
        "overrides": [{"unit_id": candidate["unit_id"], "start": candidate["start"],
                       "end": candidate["end"], "expected": "ABC", "reading": "エービーシー"}],
    })
    assert all(unit["spoken_text"] == "" for unit in result["units"] if unit["note_control"])
    assert [chunk["text"] for chunk in plan_chunks(result["units"])] == ["エービーシーを確認。"]
    assert result["unresolved_candidates"] == []


def test_reading_override_cannot_replace_a_playback_control():
    units = parse_timed_notes(note_units(["【動画を再生】\n【0:30付近】\n確認します。"]))["units"]
    control = units[1]
    with pytest.raises(VPError):
        apply_decisions(units, [], "revision", {
            "schema_version": 1, "source_revision": "revision",
            "overrides": [{"unit_id": control["id"], "start": 0,
                           "end": len(control["source_text"].rstrip()),
                           "expected": control["source_text"].rstrip(), "reading": "サンジュウビョウ"}],
        })


@pytest.mark.parametrize("notes", [
    "【再生前】\n説明だけです。",
    "【動画を再生】\n【動画を再生】",
    "【動画を再生】\n【0:30付近】\n説明。\n【0:30付近】\n重複。",
    "【動画を再生】\n【1:45付近】\n説明。\n【0:30付近】\n逆順。",
    "【動画を再生】\n【再生後】\n【0:30付近】\n説明。",
    "【動画を再生】\n【0:60付近】\n説明。",
    "【動画を再生】\n【０：３０付近】\n説明。",
    "【動画を再生】\n【0:30付近】説明。",
])
def test_ambiguous_controls_stop_before_synthesis(notes):
    with pytest.raises(VPError) as failure:
        parse_timed_notes(note_units([notes]))
    assert failure.value.code == "needs_decision"


def test_plain_notes_are_not_rewritten_into_timed_child_units():
    units = note_units(["普通の説明。\n改行も保存。", "", "次の段落。"])
    result = parse_timed_notes(units)
    assert result["timeline"] is None
    assert result["units"] == units
    assert result["units"] is not units


@pytest.mark.parametrize("frame_offset", [0, 3, 4, 5])
def test_sample_rounding_does_not_add_whole_frames_to_a_frame_aligned_clip(frame_offset):
    rate, fps = 48000, 29
    sample_offset = round(frame_offset * rate / fps)
    plan = plan_timeline({"id": "256"}, [], [], {}, 1.0, rate, fps, lead=0, tail=0,
                         frame_offset=frame_offset, sample_offset=sample_offset)
    assert plan["clip_start_frame"] == 0
    assert plan["clip_end_frame"] == 29
    assert plan["frames"] == 29
    assert plan["samples"] == round((frame_offset + 29) * rate / fps) - sample_offset


def test_required_mix_headroom_is_explicit_and_prevents_sample_clipping():
    original = np.full((4800, 2), 0.9)
    speech = np.full((4800, 1), 0.9)
    plan = {"sample_rate": 48000, "samples": 4800, "clip_start_sample": 0, "clip_end_sample": 4800,
            "narration_intervals": [{"chunk_id": "c1", "start_sample": 0, "end_sample": 4800}]}
    result, evidence = mix_audio(plan, {"c1": speech}, original, duck_db=0)
    assert evidence["peak_before_headroom"] == pytest.approx(1.8)
    assert evidence["headroom_gain"] == pytest.approx(0.98 / 1.8)
    assert np.max(np.abs(result)) == pytest.approx(0.98)
    assert np.all(original == 0.9) and np.all(speech == 0.9)


def make_review_pptx(path, media=b"fixture video bytes", *, notes=None, external=False,
                     legacy=True, shape_extra="", second_shape=""):
    """Independent minimal OOXML fixture; media decode belongs to export tests."""
    p = "http://schemas.openxmlformats.org/presentationml/2006/main"
    a = "http://schemas.openxmlformats.org/drawingml/2006/main"
    r = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
    p14 = "http://schemas.microsoft.com/office/powerpoint/2010/main"
    notes = notes if notes is not None else ["【動画を再生】"]
    media_ref = 'r:link="movie"' if external else 'r:embed="movie"'
    legacy_xml = '<a:videoFile r:link="movie"/>' if legacy else ""
    shape = f'''<p:pic><p:nvPicPr><p:cNvPr id="7" name="Review movie"/>
      <p:cNvPicPr/><p:nvPr>{legacy_xml}<p:extLst><p:ext uri="review">
      <p14:media {media_ref}/></p:ext></p:extLst></p:nvPr></p:nvPicPr>
      <p:spPr><a:xfrm><a:off x="800000" y="450000"/>
      <a:ext cx="1600000" cy="900000"/></a:xfrm>
      <a:prstGeom prst="rect"><a:avLst/></a:prstGeom>{shape_extra}</p:spPr></p:pic>'''
    target = "https://example.invalid/movie.mp4" if external else "../media/movie.mp4"
    target_mode = ' TargetMode="External"' if external else ""
    paragraphs = "".join(f"<a:p><a:r><a:t>{escape(text)}</a:t></a:r></a:p>" for text in notes)
    parts = {
        "[Content_Types].xml": '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
                               '<Default Extension="mp4" ContentType="video/mp4"/></Types>',
        "ppt/presentation.xml": f'<p:presentation xmlns:p="{p}" xmlns:r="{r}"><p:sldIdLst>'
                                '<p:sldId id="256" r:id="slide"/></p:sldIdLst>'
                                '<p:sldSz cx="3200000" cy="1800000"/></p:presentation>',
        "ppt/_rels/presentation.xml.rels": f'<Relationships><Relationship Id="slide" Type="{r}/slide" '
                                            'Target="slides/slide1.xml"/></Relationships>',
        "ppt/slides/slide1.xml": f'<p:sld xmlns:p="{p}" xmlns:a="{a}" xmlns:r="{r}" xmlns:p14="{p14}">'
                                f'<p:cSld><p:spTree>{shape}{second_shape}</p:spTree></p:cSld></p:sld>',
        "ppt/slides/_rels/slide1.xml.rels": f'<Relationships><Relationship Id="movie" Type="{r}/video" '
                                          f'Target="{target}"{target_mode}/><Relationship Id="notes" '
                                          f'Type="{r}/notesSlide" Target="../notesSlides/notes1.xml"/>'
                                          '</Relationships>',
        "ppt/notesSlides/notes1.xml": f'<p:notes xmlns:p="{p}" xmlns:a="{a}"><p:cSld><p:spTree>'
                                      '<p:sp><p:nvSpPr><p:nvPr><p:ph type="body"/></p:nvPr></p:nvSpPr>'
                                      f'<p:txBody>{paragraphs}</p:txBody></p:sp>'
                                      '</p:spTree></p:cSld></p:notes>',
        "ppt/media/movie.mp4": media,
    }
    with ZipFile(path, "w") as archive:
        for name, content in parts.items():
            archive.writestr(name, content)


def test_legacy_and_modern_references_are_one_video_placement(tmp_path):
    path = tmp_path / "source.pptx"
    make_review_pptx(path)
    result = read_pptx(path)
    assert len(result["slides"][0]["videos"]) == 1
    video = result["slides"][0]["videos"][0]
    assert video["unsupported"] == []
    assert video["part"] == "ppt/media/movie.mp4"
    assert video["x_emu"] == 800000
    assert result["presentation_size"] == {"width_emu": 3200000, "height_emu": 1800000}


@pytest.mark.parametrize("legacy", [False, True])
def test_external_media_cannot_disappear_from_unsupported_inventory(tmp_path, legacy):
    path = tmp_path / "external.pptx"
    make_review_pptx(path, external=True, legacy=legacy)
    slide = read_pptx(path)["slides"][0]
    assert slide["videos"], "External modern media must not become a silent static-slide fallback"
    assert any("external" in reason for video in slide["videos"] for reason in video["unsupported"])


def test_visible_video_border_requires_explicit_unsupported_decision(tmp_path):
    path = tmp_path / "border.pptx"
    make_review_pptx(path, shape_extra='<a:ln w="127000"><a:solidFill><a:srgbClr val="FFFFFF"/>'
                                     '</a:solidFill></a:ln>')
    video = read_pptx(path)["slides"][0]["videos"][0]
    assert any("border" in reason or "line" in reason for reason in video["unsupported"])


def media_command(arguments):
    return subprocess.run(arguments, check=True, capture_output=True, timeout=60)


def make_real_video_job(directory, *, with_audio=True, notes=None, narration_duration=0.3):
    """Use distinct frequencies and frame colors as independent timeline oracles."""
    fitz = pytest.importorskip("pymupdf")
    for binary in ("ffmpeg", "ffprobe", "pdfinfo", "pdftoppm"):
        if not shutil.which(binary):
            pytest.skip(f"{binary} unavailable")
    directory.mkdir(parents=True, exist_ok=True)
    clip = directory / "input-video.mp4"
    arguments = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
                 "-f", "lavfi", "-i", "color=c=red:s=160x90:r=10:d=1",
                 "-f", "lavfi", "-i", "color=c=blue:s=160x90:r=10:d=1"]
    if with_audio:
        arguments += ["-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=2"]
    arguments += ["-filter_complex", "[0:v][1:v]concat=n=2:v=1:a=0[v]", "-map", "[v]"]
    if with_audio:
        arguments += ["-map", "2:a:0", "-c:a", "aac", "-b:a", "192k"]
    arguments += ["-c:v", "libx264", "-preset", "ultrafast", "-threads", "1",
                  "-pix_fmt", "yuv420p", "-t", "2", str(clip)]
    media_command(arguments)
    notes = notes if notes is not None else [
        "【再生前】", "はじめに。", "【動画を再生】", "【0:01付近】", "途中です。", "【再生後】", "おわりです。",
    ]
    source = directory / "source.pptx"
    make_review_pptx(source, clip.read_bytes(), notes=notes)
    inventory = read_pptx(source)
    chunks = plan_chunks(inventory["units"])
    job = {**inventory, "source": {"copy": source.name}, "source_revision": sha256(source),
           "chunks": chunks, "renders": {}, "qa": {},
           "options": {"paragraph_pause": 0.0, "slide_lead": 0.2, "slide_tail": 0.2,
                       "blank_duration": 0.2}}
    rate = 48000
    samples = 0.2 * np.sin(2 * np.pi * 880 * np.arange(round(narration_duration * rate)) / rate)
    (directory / "renders").mkdir()
    for chunk in chunks:
        path = directory / "renders" / f"{chunk['id']}.wav"
        sf.write(path, samples, rate, subtype="PCM_24")
        checksum = sha256(path)
        job["renders"][chunk["id"]] = {"path": str(path.relative_to(directory)), "sha256": checksum}
        job["qa"][chunk["id"]] = {"status": "pass", "evidence_key": evidence_key(checksum, chunk["text"])}
    pdf = directory / "slides.pdf"
    document = fitz.open()
    page = document.new_page(width=320, height=180)
    page.draw_rect(page.rect, color=None, fill=(0, 1, 0), width=0)
    page.draw_rect(fitz.Rect(80, 45, 240, 135), color=None, fill=(1, 0, 0), width=0)
    document.save(pdf)
    document.close()
    return job, pdf


def decode_audio(path):
    raw = media_command(["ffmpeg", "-v", "error", "-nostdin", "-i", str(path), "-vn",
                         "-ar", "48000", "-ac", "2", "-f", "f32le", "pipe:1"]).stdout
    return np.frombuffer(raw, dtype="<f4").reshape(-1, 2)


def tone_amplitude(samples, seconds, frequency):
    window = samples[round((seconds - 0.04) * 48000):round((seconds + 0.04) * 48000), 0]
    phase = 2 * np.pi * frequency * np.arange(len(window)) / 48000
    return 2 * abs(np.dot(window, np.exp(-1j * phase))) / len(window)


def decode_frame(path, seconds, *, width=320, height=180):
    raw = media_command(["ffmpeg", "-v", "error", "-nostdin", "-ss", str(seconds),
                         "-i", str(path), "-frames:v", "1", "-f", "rawvideo",
                         "-pix_fmt", "rgb24", "pipe:1"]).stdout
    return np.frombuffer(raw, dtype=np.uint8).reshape(height, width, 3)


def test_real_video_relative_cue_ducking_restoration_and_last_frame_hold(tmp_path):
    directory = tmp_path / "job"
    job, pdf = make_real_video_job(directory)
    source_before = sha256(directory / "source.pptx")
    wave_before = {name: record["sha256"] for name, record in job["renders"].items()}
    result = export_video(directory, job, pdf=pdf, fps=10, resolution="320x180", duck_db=-12)
    record = result["slides"][0]["embedded_video"]
    assert record["clip_start_frame"] == 5
    assert record["clip_end_frame"] == 25
    assert record["clip_start_sample"] == 24000
    assert record["clip_end_sample"] == 120000
    assert record["headroom_gain"] == pytest.approx(1)
    assert result["quality"] == "draft"
    assert result["visual_review"] == "required"
    cue = next(item for item in record["narration_intervals"] if item["phase"] == "video")
    assert cue["start_sample"] == 72000
    assert cue["end_sample"] == 86400
    assert result["video"]["frames"] == 30
    path = directory / result["video"]["path"]
    samples = decode_audio(path)
    before_level = tone_amplitude(samples, 0.35, 880)
    baseline = tone_amplitude(samples, 0.9, 440)
    during = tone_amplitude(samples, 1.65, 440)
    restored = tone_amplitude(samples, 2.2, 440)
    assert before_level == pytest.approx(0.2, abs=0.015)
    assert baseline == pytest.approx(0.125, abs=0.015)
    assert during / baseline == pytest.approx(10 ** (-12 / 20), abs=0.04)
    assert restored / baseline == pytest.approx(1, abs=0.04)
    assert tone_amplitude(samples, 1.65, 880) == pytest.approx(before_level, abs=0.015)
    assert tone_amplitude(samples, 2.65, 880) == pytest.approx(before_level, abs=0.015)
    assert tone_amplitude(samples, 0.35, 440) < 0.006
    assert tone_amplitude(samples, 2.65, 440) < 0.006
    for seconds, channel in ((0.3, 0), (0.9, 0), (1.7, 2), (2.6, 2)):
        image = decode_frame(path, seconds)
        assert image[90, 160, channel] > 225
        assert image[15, 15, 1] > 225
    assert sha256(directory / "source.pptx") == source_before
    assert {name: sha256(directory / record["path"]) for name, record in job["renders"].items()} == wave_before


@pytest.mark.parametrize("notes", [[], ["【動画を再生】"]])
def test_no_audio_no_narration_clip_still_plays_with_silent_output(tmp_path, notes):
    directory = tmp_path / "job"
    job, pdf = make_real_video_job(directory, with_audio=False, notes=notes)
    assert job["chunks"] == []
    result = export_video(directory, job, pdf=pdf, fps=10, resolution="320x180")
    embedded = result["slides"][0]["embedded_video"]
    assert embedded["clip_start_frame"] == 2
    assert embedded["clip_end_frame"] == 22
    assert result["video"]["frames"] == 24
    path = directory / result["video"]["path"]
    samples = decode_audio(path)
    assert np.max(np.abs(samples)) < 1e-6
    assert decode_frame(path, 0.5)[90, 160, 0] > 225
    assert decode_frame(path, 1.5)[90, 160, 2] > 225


@pytest.mark.parametrize("notes,duration", [
    (["【動画を再生】", "【0:00付近】", "長い説明。", "【0:01付近】", "次の説明。"], 1.2),
    (["【動画を再生】", "【0:01付近】", "最後を越える説明。"], 1.2),
    (["【動画を再生】", "【0:03付近】", "動画の外の説明。"], 0.3),
])
def test_timed_narration_that_does_not_fit_stops_without_publishing(tmp_path, notes, duration):
    directory = tmp_path / "job"
    job, pdf = make_real_video_job(directory, notes=notes, narration_duration=duration)
    before = sha256(directory / "source.pptx")
    with pytest.raises(VPError) as failure:
        export_video(directory, job, pdf=pdf, fps=10, resolution="320x180")
    assert failure.value.code == "needs_decision"
    assert not (directory / "video/presentation.mp4").exists()
    assert sha256(directory / "source.pptx") == before


@pytest.mark.parametrize("change", ["position", "timestamp", "source_text"])
def test_timed_export_rejects_job_metadata_that_differs_from_original_pptx(tmp_path, change):
    directory = tmp_path / "job"
    job, pdf = make_real_video_job(directory)
    if change == "position":
        job["slides"][0]["videos"][0]["x_emu"] += 100000
    elif change == "timestamp":
        job["slides"][0]["timeline"]["cues"][0]["at_seconds"] = 0
    else:
        next(unit for unit in job["units"] if not unit.get("note_control"))["source_text"] = "改変した原稿。"
    with pytest.raises(VPError):
        export_video(directory, job, pdf=pdf, fps=10, resolution="320x180")
    assert not (directory / "video/presentation.mp4").exists()


@pytest.mark.parametrize("include_hidden", [False, True])
@pytest.mark.parametrize("fps,visible_frames,hidden_frames,intro_frames", [(10, 32, 34, 2), (29, 94, 100, 6)])
def test_whole_presentation_keeps_static_video_hidden_order_and_audio_offsets(
    tmp_path, include_hidden, fps, visible_frames, hidden_frames, intro_frames
):
    fitz = pytest.importorskip("pymupdf")
    directory = tmp_path / "job"
    job, video_pdf = make_real_video_job(directory)
    source = directory / "source.pptx"
    with ZipFile(source) as archive:
        parts = {name: archive.read(name) for name in archive.namelist()}
    p = "http://schemas.openxmlformats.org/presentationml/2006/main"
    r = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
    parts["ppt/presentation.xml"] = parts["ppt/presentation.xml"].decode().replace(
        '<p:sldId id="256" r:id="slide"/>',
        '<p:sldId id="257" r:id="intro"/><p:sldId id="256" r:id="slide"/>'
        '<p:sldId id="258" r:id="hidden"/>',
    ).encode()
    parts["ppt/_rels/presentation.xml.rels"] = parts["ppt/_rels/presentation.xml.rels"].decode().replace(
        '</Relationships>', f'<Relationship Id="intro" Type="{r}/slide" Target="slides/intro.xml"/>'
        f'<Relationship Id="hidden" Type="{r}/slide" Target="slides/hidden.xml"/></Relationships>',
    ).encode()
    parts["ppt/slides/intro.xml"] = f'<p:sld xmlns:p="{p}"><p:cSld><p:spTree/></p:cSld></p:sld>'.encode()
    parts["ppt/slides/hidden.xml"] = f'<p:sld xmlns:p="{p}" show="0"><p:cSld><p:spTree/></p:cSld></p:sld>'.encode()
    with ZipFile(source, "w") as archive:
        for name, content in parts.items():
            archive.writestr(name, content)
    job.update(read_pptx(source))
    job["source_revision"] = sha256(source)
    document = fitz.open()
    intro = document.new_page(width=320, height=180)
    intro.draw_rect(intro.rect, color=(0, 0, 1), fill=(0, 0, 1))
    with fitz.open(video_pdf) as original_page:
        document.insert_pdf(original_page)
    hidden = document.new_page(width=320, height=180)
    hidden.draw_rect(hidden.rect, color=(1, 0, 1), fill=(1, 0, 1))
    pdf = directory / "all-slides.pdf"
    document.save(pdf)
    document.close()
    assemble(directory, job)
    assembly_before = {path.name: sha256(path) for path in (directory / "audio").glob("*.wav")}
    result = export_video(directory, job, pdf=pdf, fps=fps, resolution="320x180",
                          include_hidden=include_hidden)
    assert {path.name: sha256(path) for path in (directory / "audio").glob("*.wav")} == assembly_before, (
        "Exporting mixed static/video slides must not overwrite the existing complete narration assembly"
    )
    assert [slide["slide_id"] for slide in result["slides"]] == (
        ["257", "256", "258"] if include_hidden else ["257", "256"]
    )
    assert result["slides"][1]["start_frame"] == intro_frames
    assert result["video"]["frames"] == (hidden_frames if include_hidden else visible_frames)
    assert result["video"]["duration"] == pytest.approx(result["video"]["frames"] / fps, abs=1e-5)
    assert result["video"]["audio_video_difference"] <= 1 / 48000 + 1e-6
    path = directory / result["video"]["path"]
    assert decode_frame(path, 0.1)[90, 160, 2] > 225
    assert decode_frame(path, 1.1)[90, 160, 0] > 225
    assert decode_frame(path, 1.9)[90, 160, 2] > 225
    if include_hidden:
        last = decode_frame(path, 3.3)
        assert last[90, 160, 0] > 225 and last[90, 160, 2] > 225
    samples = decode_audio(path)
    assert np.max(np.abs(samples[:round(0.15 * 48000)])) < 1e-5
    assert tone_amplitude(samples, 0.55, 880) == pytest.approx(0.2, abs=0.015)
    assert tone_amplitude(samples, 1.85, 880) == pytest.approx(0.2, abs=0.015)
