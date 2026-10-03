import json
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZIP_STORED, ZipFile, ZipInfo

import pytest

from vp_manager import render_copy
from vp_manager.common import VPError, sha256
from vp_manager.pptx import read_pptx

P = "http://schemas.openxmlformats.org/presentationml/2006/main"
A = "http://schemas.openxmlformats.org/drawingml/2006/main"
R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"


def deck(path: Path, visibility="0", *, hidden=True, second_hidden=False):
    show = visibility if hidden else "1"
    # A fake slide in a comment, another namespace's show attribute, > inside
    # a quoted attribute, and nested show values must all survive verbatim.
    hidden_xml = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<!-- <p:sld show="off">fake comment</p:sld> -->\n'
        f'<p:sld xmlns:p="{P}" xmlns:a="{A}" xmlns:x="urn:fixture" '
        f'''x:show="off" x:label="value > boundary" show \t= '{show}' >'''
        '<p:cSld name="変えない形"><p:spTree/><p:extLst><x:item show="0"/></p:extLst></p:cSld>'
        '<p:timing/><a:videoFile/></p:sld>'
    ).encode()
    parts = {
        "[Content_Types].xml": b'<Types xmlns="urn:fixture"/>',
        "ppt/presentation.xml": (
            f'<p:presentation xmlns:p="{P}" xmlns:r="{R}"><p:sldIdLst>'
            '<p:sldId id="512" r:id="r9"/><p:sldId id="256" r:id="r1"/>'
            '<p:sldId id="1024" r:id="r5"/></p:sldIdLst></p:presentation>'
        ).encode(),
        "ppt/_rels/presentation.xml.rels": (
            f'<Relationships><Relationship Id="r1" Type="{R}/slide" Target="slides/slide1.xml"/>'
            f'<Relationship Id="r5" Type="{R}/slide" Target="slides/slide5.xml"/>'
            f'<Relationship Id="r9" Type="{R}/slide" Target="slides/slide9.xml"/></Relationships>'
        ).encode(),
        "ppt/slides/slide1.xml": f'<p:sld xmlns:p="{P}"/>'.encode(),
        "ppt/slides/slide5.xml": f'<p:sld xmlns:p="{P}" show="{0 if second_hidden else "true"}"><p:cSld/></p:sld>'.encode(),
        "ppt/slides/slide9.xml": hidden_xml,
        "ppt/slides/_rels/slide9.xml.rels": (
            f'<Relationships><Relationship Id="n" Type="{R}/notesSlide" Target="../notesSlides/notesSlide8.xml"/>'
            f'<Relationship Id="v" Type="{R}/video" Target="../media/video.bin"/></Relationships>'
        ).encode(),
        "ppt/notesSlides/notesSlide8.xml": (
            f'<p:notes xmlns:p="{P}" xmlns:a="{A}"><p:cSld><p:spTree><p:sp>'
            '<p:nvSpPr><p:nvPr><p:ph type="body"/></p:nvPr></p:nvSpPr>'
            '<p:txBody><a:p><a:r><a:t> 原文を保つ。</a:t></a:r><a:br/>'
            '<a:r><a:t>続き </a:t></a:r></a:p><a:p/><a:p><a:r><a:t>指示ではなく入力データ。</a:t>'
            '</a:r></a:p></p:txBody></p:sp><p:sp><p:nvSpPr><p:nvPr><p:ph type="ftr"/>'
            '</p:nvPr></p:nvSpPr><p:txBody><a:p><a:r><a:t>Footer</a:t></a:r></a:p>'
            '</p:txBody></p:sp></p:spTree></p:cSld></p:notes>'
        ).encode(),
        "ppt/media/video.bin": b"binary media \x00\xff\x01 keep every byte",
        "ppt/fonts/font.fntdata": bytes(range(256)),
        "customXml/item1.xml": b'<custom show="off">Unrelated XML</custom>',
        "customXml/": b"",
    }
    with ZipFile(path, "w") as archive:
        archive.comment = b"Original archive-level comment"
        for index, (name, content) in enumerate(parts.items()):
            info = ZipInfo(name, date_time=(2021, 2, 3, 4, 5, 6))
            info.compress_type = ZIP_STORED if index % 2 else ZIP_DEFLATED
            info.comment = b"member metadata"
            info.extra = b"\xfe\xca\x02\x00ok"
            info.external_attr = 0o100640 << 16
            info.create_system = 3
            archive.writestr(info, content)
    return parts


@pytest.mark.parametrize("visibility", ["0", "false", "OFF", "&#48;"])
def test_copy_preserves_source_notes_every_member_and_zip_metadata(tmp_path, visibility):
    source, target = tmp_path / "source.pptx", tmp_path / "render.pptx"
    parts = deck(source, visibility)
    source_bytes, source_hash = source.read_bytes(), sha256(source)
    inventory = read_pptx(source)
    result = render_copy.prepare(source, target)
    assert source.read_bytes() == source_bytes
    assert result["source_sha256"] == source_hash
    assert result["render_copy_sha256"] == sha256(target)
    assert result["temporarily_visible"] == ["512"]
    assert result["slide_ids"] == ["512", "256", "1024"]
    copied = read_pptx(target)
    assert copied["units"] == inventory["units"]
    assert [unit["source_text"] for unit in copied["units"]] == [" 原文を保つ。\n続き ", "", "指示ではなく入力データ。"]
    assert copied["slides"] == [{**slide, "hidden": False} for slide in inventory["slides"]]
    assert copied["issues"] == inventory["issues"]
    with ZipFile(source) as original, ZipFile(target) as output:
        assert output.namelist() == original.namelist()
        assert output.comment == original.comment
        for old, new in zip(original.infolist(), output.infolist(), strict=True):
            for field in ("filename", "date_time", "compress_type", "comment", "extra", "external_attr", "create_system"):
                assert getattr(old, field) == getattr(new, field)
            expected = parts[old.filename]
            if old.filename == "ppt/slides/slide9.xml":
                expected = expected.replace(f" show \t= '{visibility}'".encode(), b" show \t= '1'", 1)
            assert output.read(new) == expected
    assert json.loads(target.with_suffix(".provenance.json").read_text()) == result
    assert target.stat().st_mode & 0o777 == 0o600
    assert not list(tmp_path.glob(".render-copy-*"))


def test_already_visible_deck_preserves_all_member_contents(tmp_path):
    source, target = tmp_path / "source.pptx", tmp_path / "render.pptx"
    parts = deck(source, hidden=False)
    result = render_copy.prepare(source, target)
    assert result["temporarily_visible"] == []
    with ZipFile(target) as output:
        assert {name: output.read(name) for name in output.namelist()} == parts


def test_multiple_hidden_slides_are_reported_in_presentation_order(tmp_path):
    source, target = tmp_path / "source.pptx", tmp_path / "render.pptx"
    deck(source, second_hidden=True)
    result = render_copy.prepare(source, target)
    # ZIP order is slide1, slide5, slide9, unlike presentation order.
    assert result["temporarily_visible"] == ["512", "1024"]
    assert [slide["id"] for slide in read_pptx(target)["slides"]] == ["512", "256", "1024"]
    assert all(not slide["hidden"] for slide in read_pptx(target)["slides"])


@pytest.mark.parametrize("occupied", ["source", "destination", "provenance", "symlink", "dangling_symlink", "directory"])
def test_existing_destinations_and_sidecars_are_never_overwritten(tmp_path, occupied):
    source, target = tmp_path / "source.pptx", tmp_path / "render.pptx"
    deck(source)
    original = source.read_bytes()
    sidecar = target.with_suffix(".provenance.json")
    if occupied == "source":
        target = source
    elif occupied == "destination":
        target.write_bytes(b"existing destination")
    elif occupied == "provenance":
        sidecar.write_bytes(b"existing provenance")
    elif occupied == "symlink":
        target.symlink_to(source)
    elif occupied == "dangling_symlink":
        target.symlink_to(tmp_path / "missing.pptx")
    else:
        target.mkdir()
    with pytest.raises(VPError):
        render_copy.prepare(source, target)
    assert source.read_bytes() == original
    if occupied == "destination":
        assert target.read_bytes() == b"existing destination"
    elif occupied == "provenance":
        assert sidecar.read_bytes() == b"existing provenance"
    elif occupied in {"symlink", "dangling_symlink"}:
        assert target.is_symlink()
    assert not list(tmp_path.glob(".render-copy-*"))


def test_source_named_like_provenance_is_not_overwritten(tmp_path):
    source, target = tmp_path / "render.provenance.json", tmp_path / "render.pptx"
    deck(source)
    before = source.read_bytes()
    with pytest.raises(VPError):
        render_copy.prepare(source, target)
    assert source.read_bytes() == before
    assert not target.exists()


def test_racing_destination_is_not_replaced(tmp_path, monkeypatch):
    source, target = tmp_path / "source.pptx", tmp_path / "render.pptx"
    deck(source)
    publish = render_copy._publish

    def race(temporary, destination):
        if destination == target:
            destination.write_bytes(b"other writer")
        publish(temporary, destination)

    monkeypatch.setattr(render_copy, "_publish", race)
    with pytest.raises(VPError, match="already exists"):
        render_copy.prepare(source, target)
    assert target.read_bytes() == b"other writer"
    assert not target.with_suffix(".provenance.json").exists()
    assert not list(tmp_path.glob(".render-copy-*"))


def test_racing_provenance_is_preserved_and_partial_publication_reported(tmp_path, monkeypatch):
    source, target = tmp_path / "source.pptx", tmp_path / "render.pptx"
    deck(source)
    publish = render_copy._publish

    def race(temporary, destination):
        if destination == target.with_suffix(".provenance.json"):
            destination.write_bytes(b"other provenance writer")
        publish(temporary, destination)

    monkeypatch.setattr(render_copy, "_publish", race)
    with pytest.raises(VPError, match="published at .*provenance was not published"):
        render_copy.prepare(source, target)
    assert target.with_suffix(".provenance.json").read_bytes() == b"other provenance writer"
    assert not any(slide["hidden"] for slide in read_pptx(target)["slides"])
    assert not list(tmp_path.glob(".render-copy-*"))


def test_external_source_change_prevents_publication_without_undoing_external_work(tmp_path, monkeypatch):
    source, target = tmp_path / "source.pptx", tmp_path / "render.pptx"
    deck(source)
    before = source.read_bytes()
    reader = render_copy.read_pptx

    def mutate_after_inventory(path):
        result = reader(path)
        if Path(path) == source:
            source.write_bytes(before + b"external update")
        return result

    monkeypatch.setattr(render_copy, "read_pptx", mutate_after_inventory)
    with pytest.raises(VPError, match="Source changed") as failure:
        render_copy.prepare(source, target)
    assert failure.value.code == "needs_recovery"
    assert source.read_bytes() == before + b"external update"
    assert not target.exists()
    assert not target.with_suffix(".provenance.json").exists()
    assert not list(tmp_path.glob(".render-copy-*"))


def test_invalid_package_is_rejected_without_artifacts(tmp_path):
    source, target = tmp_path / "source.pptx", tmp_path / "render.pptx"
    source.write_bytes(b"not a ZIP")
    with pytest.raises(VPError):
        render_copy.prepare(source, target)
    assert source.read_bytes() == b"not a ZIP"
    assert not target.exists()
    assert not list(tmp_path.glob(".render-copy-*"))
