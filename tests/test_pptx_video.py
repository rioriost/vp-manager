import hashlib
from html import escape
from zipfile import ZipFile

import pytest

from vp_manager.common import VPError
from vp_manager.pptx import extract_video, read_pptx

P = "http://schemas.openxmlformats.org/presentationml/2006/main"
A = "http://schemas.openxmlformats.org/drawingml/2006/main"
R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
P14 = "http://schemas.microsoft.com/office/powerpoint/2010/main"


def make_video_pptx(
    path,
    *,
    notes=None,
    video_bytes=b"synthetic video bytes",
    extension="mp4",
    mime="video/mp4",
    external=False,
    modern=True,
    legacy=True,
    transform="",
    crop="",
    trim="",
    grouped=False,
    multiple=False,
    overlay=False,
    audio=False,
    both_link=False,
    timing="",
    border="",
):
    """ZIP-only local parser fixture; pass real clip bytes for media integration."""
    notes = (
        notes
        if notes is not None
        else [
            "【再生前】",
            "説明です。",
            "【動画を再生】",
            "【0:30付近】",
            "注釈です。",
            "【再生後】",
            "終わりです。",
        ]
    )
    media_part = f"ppt/media/video.{extension}"
    old = f'<a:{"audioFile" if audio else "videoFile"} r:link="v"/>' if legacy else ""
    new = (
        f'<p14:media r:embed="m" {'r:link="external"' if both_link else ""}>{trim}</p14:media>'
        if modern
        else ""
    )
    picture = f"""<p:pic><p:nvPicPr><p:cNvPr id="4" name="Clip"/><p:cNvPicPr/><p:nvPr>{old}<p:extLst><p:ext uri="media">{new}</p:ext></p:extLst></p:nvPr></p:nvPicPr><p:blipFill><a:blip r:embed="poster"/>{crop}<a:stretch><a:fillRect/></a:stretch></p:blipFill><p:spPr><a:xfrm {transform}><a:off x="100" y="200"/><a:ext cx="6400" cy="3600"/></a:xfrm><a:prstGeom prst="rect"/>{border}</p:spPr></p:pic>"""
    shapes = f"<p:grpSp>{picture}</p:grpSp>" if grouped else picture
    if multiple:
        shapes += picture.replace('id="4"', 'id="5"')
    if overlay:
        shapes += '<p:sp><p:spPr><a:xfrm><a:off x="200" y="200"/><a:ext cx="100" cy="100"/></a:xfrm></p:spPr></p:sp>'
    target = "https://example.invalid/video.mp4" if external else f"../media/video.{extension}"
    mode = 'TargetMode="External"' if external else ""
    rels = f'''<Relationships><Relationship Id="v" Type="{R}/{"audio" if audio else "video"}" Target="{target}" {mode}/><Relationship Id="m" Type="http://schemas.microsoft.com/office/2007/relationships/media" Target="{target}" {mode}/><Relationship Id="poster" Type="{R}/image" Target="../media/poster.png"/><Relationship Id="notes" Type="{R}/notesSlide" Target="../notesSlides/notes1.xml"/><Relationship Id="external" Type="{R}/video" Target="https://example.invalid/other.mp4" TargetMode="External"/></Relationships>'''
    if not both_link:
        rels = rels.replace(
            f'<Relationship Id="external" Type="{R}/video" Target="https://example.invalid/other.mp4" TargetMode="External"/>',
            "",
        )
    paragraphs = "".join(
        "<a:p>"
        + "<a:br/>".join(f"<a:r><a:t>{escape(line)}</a:t></a:r>" for line in paragraph.split("\n"))
        + "</a:p>"
        for paragraph in notes
    )
    parts = {
        "[Content_Types].xml": f'<Types><Default Extension="{extension}" ContentType="{mime}"/></Types>',
        "ppt/presentation.xml": f'<p:presentation xmlns:p="{P}" xmlns:r="{R}"><p:sldIdLst><p:sldId id="256" r:id="slide"/></p:sldIdLst><p:sldSz cx="10000" cy="6000"/></p:presentation>',
        "ppt/_rels/presentation.xml.rels": f'<Relationships><Relationship Id="slide" Type="{R}/slide" Target="slides/slide1.xml"/></Relationships>',
        "ppt/slides/slide1.xml": f'<p:sld xmlns:p="{P}" xmlns:a="{A}" xmlns:r="{R}" xmlns:p14="{P14}"><p:cSld><p:spTree>{shapes}</p:spTree></p:cSld>{timing}</p:sld>',
        "ppt/slides/_rels/slide1.xml.rels": rels,
        "ppt/notesSlides/notes1.xml": f'<p:notes xmlns:p="{P}" xmlns:a="{A}"><p:cSld><p:spTree><p:sp><p:nvSpPr><p:nvPr><p:ph type="body"/></p:nvPr></p:nvSpPr><p:txBody>{paragraphs}</p:txBody></p:sp></p:spTree></p:cSld></p:notes>',
        media_part: video_bytes,
        "ppt/media/poster.png": b"poster",
    }
    with ZipFile(path, "w") as archive:
        for name, data in parts.items():
            archive.writestr(name, data)
    return path


def test_dual_relationships_one_video_timeline_and_source_unchanged(tmp_path):
    source = make_video_pptx(tmp_path / "sample.pptx")
    original = source.read_bytes()
    result = read_pptx(source)
    assert result["presentation_size"] == {"width_emu": 10000, "height_emu": 6000}
    slide = result["slides"][0]
    assert len(slide["videos"]) == 1
    video = slide["videos"][0]
    assert video["part"] == "ppt/media/video.mp4"
    assert video["shape_id"] == "4"
    assert [video[key] for key in ("x_emu", "y_emu", "width_emu", "height_emu")] == [100, 200, 6400, 3600]
    assert video["unsupported"] == []
    assert "embedded_media_not_preserved" not in slide["warnings"]
    assert video["sha256"] == hashlib.sha256(b"synthetic video bytes").hexdigest()
    assert video["poster_part"] == "ppt/media/poster.png"
    assert slide["timeline"]["cues"][0]["at_seconds"] == 30
    assert all(u["spoken_text"] == "" for u in result["units"] if u["note_control"])
    assert source.read_bytes() == original


@pytest.mark.parametrize(
    "kwargs, reason",
    [
        ({"external": True}, "external_video"),
        ({"both_link": True}, "external_video"),
        ({"transform": 'rot="5400000"'}, "rotation"),
        ({"transform": 'flipH="1"'}, "flip"),
        ({"crop": '<a:srcRect l="1000"/>'}, "crop"),
        ({"trim": '<p14:trim st="1000"/>'}, "media_trim"),
        ({"trim": '<p14:fade in="1000"/>'}, "media_fade"),
        ({"grouped": True}, "grouped_video"),
        ({"multiple": True}, "multiple_videos_on_slide"),
        ({"overlay": True}, "overlapping_foreground_shape"),
        (
            {"border": '<a:ln w="12700"><a:solidFill><a:srgbClr val="000000"/></a:solidFill></a:ln>'},
            "video_border",
        ),
        ({"mime": "audio/mp4"}, "media_content_type_is_not_video"),
        (
            {
                "timing": '<p:timing><p:video><p:cMediaNode><p:cTn repeatCount="indefinite"/><p:tgtEl><p:spTgt spid="4"/></p:tgtEl></p:cMediaNode></p:video></p:timing>'
            },
            "video_repeat",
        ),
    ],
)
def test_unsupported_layout_or_playback_is_explicit(tmp_path, kwargs, reason):
    source = make_video_pptx(tmp_path / "sample.pptx", **kwargs)
    videos = read_pptx(source)["slides"][0]["videos"]
    assert reason in videos[0]["unsupported"]
    with pytest.raises(VPError):
        extract_video(source, videos[0], tmp_path / "copy.mp4")


@pytest.mark.parametrize("modern, legacy", [(True, False), (False, True)])
def test_one_relationship_form_is_supported(tmp_path, modern, legacy):
    source = make_video_pptx(tmp_path / "sample.pptx", modern=modern, legacy=legacy)
    assert read_pptx(source)["slides"][0]["videos"][0]["unsupported"] == []


def test_audio_media_not_misclassified_as_video(tmp_path):
    source = make_video_pptx(
        tmp_path / "sample.pptx", audio=True, extension="mp3", mime="audio/mpeg", notes=["音声。"]
    )
    slide = read_pptx(source)["slides"][0]
    assert slide["videos"] == []
    assert "embedded_media_not_preserved" in slide["warnings"]


def test_extract_checks_original_bytes_and_protects_source(tmp_path):
    source = make_video_pptx(tmp_path / "sample.pptx")
    video = read_pptx(source)["slides"][0]["videos"][0]
    target = tmp_path / "clip.mp4"
    assert extract_video(source, video, target)["sha256"] == video["sha256"]
    assert target.read_bytes() == b"synthetic video bytes"
    with pytest.raises(VPError, match="overwrite"):
        extract_video(source, video, source)
    make_video_pptx(source, video_bytes=b"changed")
    with pytest.raises(VPError, match="changed"):
        extract_video(source, video, target)
    assert target.read_bytes() == b"synthetic video bytes"


@pytest.mark.parametrize("notes", [[], [""], ["  ", "\n"]])
def test_empty_video_notes_need_no_cues_or_static_silent_duration(tmp_path, notes):
    source = make_video_pptx(tmp_path / "sample.pptx", notes=notes)
    before = source.read_bytes()
    slide = read_pptx(source)["slides"][0]
    assert slide["videos"][0]["unsupported"] == []
    assert "timeline" not in slide
    assert "embedded_video_requires_timed_notes" not in slide["warnings"]
    assert "empty_notes_requires_silent_duration" not in slide["warnings"]
    assert source.read_bytes() == before


def test_narrated_video_without_cues_still_requires_timed_notes(tmp_path):
    source = make_video_pptx(tmp_path / "sample.pptx", notes=["説明があります。"])
    assert "embedded_video_requires_timed_notes" in read_pptx(source)["slides"][0]["warnings"]
