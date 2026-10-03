"""Read presentation-ordered notes without opening or modifying PowerPoint."""

import posixpath
import xml.etree.ElementTree as ET
from pathlib import Path
from urllib.parse import unquote, urlsplit
from zipfile import BadZipFile, ZipFile

from .common import VPError

_MAX_XML = 16 * 1024 * 1024


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _xml(archive: ZipFile, part: str) -> ET.Element:
    try:
        info = archive.getinfo(part)
        if info.file_size > _MAX_XML:
            raise VPError(f"PPTX XML part is too large: {part}")
        raw = archive.read(part)
        if b"<!DOCTYPE" in raw.upper() or b"<!ENTITY" in raw.upper():
            raise VPError(f"PPTX XML declarations are unsupported: {part}")
        return ET.fromstring(raw)
    except (KeyError, ET.ParseError, OSError, BadZipFile, RuntimeError) as exc:
        raise VPError(f"Unreadable PPTX part: {part}: {exc}") from exc


def _rels_part(part: str) -> str:
    folder, filename = posixpath.split(part)
    return posixpath.join(folder, "_rels", filename + ".rels")


def _resolve(part: str, target: str) -> str:
    parsed = urlsplit(target)
    if parsed.scheme or parsed.netloc or parsed.query or parsed.fragment or "\\" in target:
        raise VPError(f"Invalid internal PPTX relationship target: {target}")
    target = unquote(parsed.path)
    if "\\" in target or not target:
        raise VPError("Invalid empty or backslash PPTX relationship target")
    result = posixpath.normpath(
        target.lstrip("/") if target.startswith("/") else posixpath.join(posixpath.dirname(part), target)
    )
    if result == ".." or result.startswith("../"):
        raise VPError("PPTX relationship escapes the package")
    return result


def _relationships(archive: ZipFile, part: str, required: bool = False) -> dict:
    rels_part = _rels_part(part)
    if rels_part not in archive.namelist():
        if required:
            raise VPError(f"Missing PPTX relationship part: {rels_part}")
        return {}
    result = {}
    for rel in _xml(archive, rels_part):
        if _local(rel.tag) != "Relationship":
            continue
        ident, target, reltype = rel.get("Id"), rel.get("Target"), rel.get("Type")
        if not ident or not target or not reltype or ident in result:
            raise VPError(f"Malformed or duplicate PPTX relationship in {rels_part}")
        external = rel.get("TargetMode") == "External"
        result[ident] = {
            "type": reltype.rsplit("/", 1)[-1],
            "external": external,
            "target": target if external else _resolve(part, target),
        }
    return result


def _notes_paragraphs(archive: ZipFile, notes_part: str) -> list[str]:
    root = _xml(archive, notes_part)
    paragraphs = []
    for shape in root.iter():
        if _local(shape.tag) != "sp":
            continue
        if not any(_local(node.tag) == "ph" and node.get("type") == "body" for node in shape.iter()):
            continue
        for body in shape:
            if _local(body.tag) != "txBody":
                continue
            for paragraph in body:
                if _local(paragraph.tag) != "p":
                    continue
                # text runs and fields preserve their exact order; soft breaks
                # remain inside their source paragraph.
                paragraphs.append(
                    "".join(
                        node.text or "" if _local(node.tag) == "t" else "\n"
                        for node in paragraph.iter()
                        if _local(node.tag) in {"t", "br"}
                    )
                )
    return paragraphs


def read_pptx(path: str | Path) -> dict:
    """Inventory every slide, including hidden and empty slides, in display order."""
    result = {"units": [], "slides": [], "issues": []}
    try:
        with ZipFile(path) as archive:
            names = archive.namelist()
            if len(names) != len(set(names)):
                raise VPError("PPTX contains duplicate package part names")
            presentation_part = "ppt/presentation.xml"
            presentation = _xml(archive, presentation_part)
            rels = _relationships(archive, presentation_part, required=True)
            slide_nodes = [
                node
                for group in presentation
                if _local(group.tag) == "sldIdLst"
                for node in group
                if _local(node.tag) == "sldId"
            ]
            seen_ids, seen_parts = set(), set()
            for order, node in enumerate(slide_nodes, 1):
                slide_id = node.get("id")
                relationship_id = next(
                    (
                        value
                        for key, value in node.attrib.items()
                        if key.startswith("{") and _local(key) == "id"
                    ),
                    None,
                )
                relationship = rels.get(relationship_id)
                if (
                    not slide_id
                    or not slide_id.isascii()
                    or not slide_id.isdecimal()
                    or slide_id in seen_ids
                    or not relationship
                    or relationship["type"] != "slide"
                    or relationship["external"]
                ):
                    raise VPError("Invalid, missing or duplicate presentation slide relationship")
                part = relationship["target"]
                if part in seen_parts:
                    raise VPError("A slide part appears more than once in the presentation")
                seen_ids.add(slide_id)
                seen_parts.add(part)
                slide = _xml(archive, part)
                slide_rels = _relationships(archive, part)
                warnings = []
                tags = {_local(child.tag) for child in slide.iter()}
                if "timing" in tags or "transition" in tags:
                    warnings.append("animation_or_transition_not_preserved")
                if tags & {"videoFile", "audioFile", "media", "wavAudioFile"} or any(
                    rel["type"] in {"video", "audio", "media"} for rel in slide_rels.values()
                ):
                    warnings.append("embedded_media_not_preserved")
                notes = [rel for rel in slide_rels.values() if rel["type"] == "notesSlide"]
                if len(notes) > 1 or (notes and notes[0]["external"]):
                    raise VPError(f"Invalid notes relationship on slide {slide_id}")
                paragraphs = _notes_paragraphs(archive, notes[0]["target"]) if notes else []
                unit_ids = []
                for paragraph_index, source in enumerate(paragraphs, 1):
                    unit_id = f"u{len(result['units']) + 1:04d}"
                    result["units"].append(
                        {
                            "id": unit_id,
                            "source_text": source,
                            "slide_id": slide_id,
                            "paragraph_index": paragraph_index,
                        }
                    )
                    unit_ids.append(unit_id)
                if not any(paragraph.strip() for paragraph in paragraphs):
                    warnings.append("empty_notes_requires_silent_duration")
                result["slides"].append(
                    {
                        "id": slide_id,
                        "order": order,
                        "part": part,
                        "hidden": slide.get("show", "1").lower() in {"0", "false", "off"},
                        "unit_ids": unit_ids,
                        "warnings": warnings,
                    }
                )
                result["issues"].extend(
                    {
                        "code": warning,
                        "slide_id": slide_id,
                        "severity": "warning",
                        "message": warning.replace("_", " "),
                    }
                    for warning in warnings
                )
    except (OSError, BadZipFile) as exc:
        raise VPError(f"Cannot read PPTX: {exc}") from exc
    return result
