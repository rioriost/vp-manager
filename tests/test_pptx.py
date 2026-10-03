import hashlib
from zipfile import ZipFile

import pytest

from vp_manager.common import VPError
from vp_manager.pptx import read_pptx

P = "http://schemas.openxmlformats.org/presentationml/2006/main"
A = "http://schemas.openxmlformats.org/drawingml/2006/main"
R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"


def make_pptx(path, target="slides/slide9.xml", ident="256", notes_target="../notesSlides/notesSlide2.xml"):
    parts = {
        "ppt/presentation.xml": f'<p:presentation xmlns:p="{P}" xmlns:r="{R}"><p:sldIdLst><p:sldId id="{ident}" r:id="r2"/><p:sldId id="257" r:id="r1"/></p:sldIdLst></p:presentation>',
        "ppt/_rels/presentation.xml.rels": f'<Relationships><Relationship Id="r1" Type="{R}/slide" Target="slides/slide1.xml"/><Relationship Id="r2" Type="{R}/slide" Target="{target}"/></Relationships>',
        "ppt/slides/slide9.xml": f'<p:sld xmlns:p="{P}" xmlns:a="{A}" show="0"><p:timing/><p:cSld><a:videoFile/></p:cSld></p:sld>',
        "ppt/slides/slide1.xml": f'<p:sld xmlns:p="{P}"/>',
        "ppt/slides/_rels/slide9.xml.rels": f'<Relationships><Relationship Id="n1" Type="{R}/notesSlide" Target="{notes_target}"/></Relationships>',
        "ppt/notesSlides/notesSlide2.xml": f'''<p:notes xmlns:p="{P}" xmlns:a="{A}"><p:cSld><p:spTree>
          <p:sp><p:nvSpPr><p:nvPr><p:ph type="sldNum"/></p:nvPr></p:nvSpPr><p:txBody><a:p><a:r><a:t>99</a:t></a:r></a:p></p:txBody></p:sp>
          <p:sp><p:nvSpPr><p:nvPr><p:ph type="body"/></p:nvPr></p:nvSpPr><p:txBody><a:p><a:r><a:t> 原文</a:t></a:r><a:br/><a:r><a:t>続き </a:t></a:r></a:p><a:p/><a:p><a:r><a:t>末尾。</a:t></a:r></a:p></p:txBody></p:sp>
          <p:sp><p:nvSpPr><p:nvPr><p:ph type="ftr"/></p:nvPr></p:nvSpPr><p:txBody><a:p><a:r><a:t>Footer</a:t></a:r></a:p></p:txBody></p:sp>
          </p:spTree></p:cSld></p:notes>''',
    }
    with ZipFile(path, "w") as archive:
        for part, xml in parts.items():
            archive.writestr(part, xml)


def test_pptx_order_hidden_empty_notes_fidelity_and_original_preserved(tmp_path):
    path = tmp_path / "fixture.pptx"
    make_pptx(path)
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    result = read_pptx(path)
    assert [slide["id"] for slide in result["slides"]] == ["256", "257"]
    assert result["slides"][0]["part"] == "ppt/slides/slide9.xml"
    assert result["slides"][0]["hidden"] is True
    assert result["slides"][1]["unit_ids"] == []
    assert "empty_notes_requires_silent_duration" in result["slides"][1]["warnings"]
    assert "animation_or_transition_not_preserved" in result["slides"][0]["warnings"]
    assert "embedded_media_not_preserved" in result["slides"][0]["warnings"]
    assert [unit["source_text"] for unit in result["units"]] == [" 原文\n続き ", "", "末尾。"]
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before


@pytest.mark.parametrize(
    "kwargs",
    [
        {"target": "slides/missing.xml"},
        {"target": "../../../outside.xml"},
        {"target": "https://example.test/slide.xml"},
        {"ident": "bad-name"},
        {"ident": "257"},
        {"notes_target": "../notesSlides/missing.xml"},
    ],
)
def test_rejects_broken_or_unsafe_relationships_and_ids(tmp_path, kwargs):
    path = tmp_path / "bad.pptx"
    make_pptx(path, **kwargs)
    with pytest.raises(VPError):
        read_pptx(path)


def test_rejects_non_zip_input(tmp_path):
    path = tmp_path / "bad.pptx"
    path.write_text("not a zip")
    with pytest.raises(VPError):
        read_pptx(path)
