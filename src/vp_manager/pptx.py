"""Read presentation-ordered notes without opening or modifying PowerPoint."""

import hashlib
import os
import posixpath
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path
from urllib.parse import unquote, urlsplit
from zipfile import BadZipFile, ZipFile

from .common import VPError
from .timed_notes import parse_timed_notes

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


_VIDEO_EXTENSIONS = {"mp4", "m4v", "mov", "wmv", "avi", "webm", "mkv", "mpg", "mpeg", "ogv"}
_MAX_VIDEO = 2 * 1024 * 1024 * 1024


def _attribute(node, name):
    return next((value for key, value in node.attrib.items() if _local(key) == name), None)


def _content_types(archive):
    if "[Content_Types].xml" not in archive.namelist():
        return {}, {}
    defaults, overrides = {}, {}
    for node in _xml(archive, "[Content_Types].xml"):
        if _local(node.tag) == "Default":
            defaults[node.get("Extension", "").lower()] = node.get("ContentType", "")
        elif _local(node.tag) == "Override":
            overrides[node.get("PartName", "").lstrip("/")] = node.get("ContentType", "")
    return defaults, overrides


def _geometry(shape):
    properties = next((node for node in shape if _local(node.tag) == "spPr"), None)
    transform = (
        next((node for node in properties if _local(node.tag) == "xfrm"), None)
        if properties is not None
        else None
    )
    if transform is None:
        return None, ["missing_geometry"]
    unsupported = []
    if transform.get("rot", "0") != "0":
        unsupported.append("rotation")
    if any(transform.get(key, "0").lower() not in {"0", "false", "off"} for key in ("flipH", "flipV")):
        unsupported.append("flip")
    try:
        offset = next(node for node in transform if _local(node.tag) == "off")
        extent = next(node for node in transform if _local(node.tag) == "ext")
        geometry = {
            "x_emu": int(offset.attrib["x"]),
            "y_emu": int(offset.attrib["y"]),
            "width_emu": int(extent.attrib["cx"]),
            "height_emu": int(extent.attrib["cy"]),
        }
        if min(geometry["width_emu"], geometry["height_emu"]) <= 0:
            raise ValueError("nonpositive dimensions")
        return geometry, unsupported
    except (StopIteration, KeyError, ValueError):
        return None, unsupported + ["invalid_geometry"]


def _part_digest(archive, part):
    info = archive.getinfo(part)
    if info.file_size > _MAX_VIDEO:
        raise VPError("Embedded video exceeds the 2 GiB supported size")
    digest = hashlib.sha256()
    with archive.open(part) as stream:
        while block := stream.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest(), info.file_size


def _video_inventory(archive, slide, relationships, content_types, size):
    defaults, overrides = content_types
    parents = {child: parent for parent in slide.iter() for child in parent}
    shapes = [node for node in slide.iter() if _local(node.tag) in {"pic", "sp", "graphicFrame", "cxnSp"}]
    videos = []
    for z_order, shape in enumerate(shapes):
        markers = [node for node in shape.iter() if _local(node.tag) in {"media", "videoFile", "audioFile"}]
        if not markers:
            continue
        modern = [node for node in markers if _local(node.tag) == "media"]
        legacy = [node for node in markers if _local(node.tag) == "videoFile"]
        refs = []
        # MS-PPTX: r:link takes precedence over r:embed when both are supplied.
        for node in modern or legacy:
            ident = _attribute(node, "link") or _attribute(node, "embed")
            refs.append(relationships.get(ident))
        local_parts = {rel["target"] for rel in refs if rel and not rel["external"]}
        part = next(iter(local_parts)) if len(local_parts) == 1 else None
        mime = overrides.get(part, defaults.get(part.rsplit(".", 1)[-1].lower(), "")) if part else ""
        is_video = (
            bool(legacy)
            or mime.startswith("video/")
            or any(rel and rel["type"] == "video" for rel in refs)
            or any(
                rel and urlsplit(rel["target"]).path.rsplit(".", 1)[-1].lower() in _VIDEO_EXTENSIONS
                for rel in refs
            )
        )
        audio_extensions = {"mp3", "wav", "m4a", "aac", "flac", "wma", "ogg"}
        known_audio = (
            any(_local(node.tag) == "audioFile" for node in markers)
            or mime.startswith("audio/")
            or bool(refs)
            and all(
                rel and urlsplit(rel["target"]).path.rsplit(".", 1)[-1].lower() in audio_extensions
                for rel in refs
            )
        )
        ambiguous = bool(modern) and not is_video and not known_audio
        if not is_video and not ambiguous:
            continue
        record = {
            "part": part,
            "shape_id": None,
            "x_emu": None,
            "y_emu": None,
            "width_emu": None,
            "height_emu": None,
            "content_type": mime or None,
            "sha256": None,
            "size_bytes": None,
            "poster_part": None,
            "z_order": z_order,
            "unsupported": [],
        }
        unsupported = record["unsupported"]
        if ambiguous:
            unsupported.append("unclassified_media")
        identity = next((node for node in shape.iter() if _local(node.tag) == "cNvPr"), None)
        record["shape_id"] = identity.get("id") if identity is not None else None
        if not refs or any(rel is None for rel in refs):
            unsupported.append("missing_media_relationship")
        if any(rel and rel["external"] for rel in refs):
            unsupported.append("external_video")
            record["external_targets"] = [rel["target"] for rel in refs if rel and rel["external"]]
        if len(local_parts) > 1:
            unsupported.append("ambiguous_media_relationships")
        if any(rel and rel["type"] not in {"media", "video"} for rel in refs):
            unsupported.append("invalid_media_relationship_type")
        if mime and not mime.startswith("video/"):
            unsupported.append("media_content_type_is_not_video")
        if part:
            try:
                record["size_bytes"] = archive.getinfo(part).file_size
                if record["size_bytes"] > _MAX_VIDEO:
                    unsupported.append("video_exceeds_2gib")
                else:
                    record["sha256"], record["size_bytes"] = _part_digest(archive, part)
            except KeyError:
                unsupported.append("missing_media_part")
        else:
            unsupported.append("missing_embedded_video")
        geometry, transforms = _geometry(shape)
        if geometry:
            record.update(geometry)
        unsupported.extend(transforms)
        if size is None:
            unsupported.append("missing_slide_size")
        elif geometry and (
            geometry["x_emu"] < 0
            or geometry["y_emu"] < 0
            or geometry["x_emu"] + geometry["width_emu"] > size["width_emu"]
            or geometry["y_emu"] + geometry["height_emu"] > size["height_emu"]
        ):
            unsupported.append("video_outside_slide")
        ancestor = parents.get(shape)
        while ancestor is not None:
            if _local(ancestor.tag) == "grpSp":
                unsupported.append("grouped_video")
                break
            ancestor = parents.get(ancestor)
        for node in shape.iter():
            tag = _local(node.tag)
            if tag in {"srcRect", "fillRect"} and any(value != "0" for value in node.attrib.values()):
                unsupported.append("crop")
            if tag in {"trim", "fade"} and any(value not in {"0", "0.0"} for value in node.attrib.values()):
                unsupported.append("media_" + tag)
            if tag == "prstGeom" and node.get("prst") != "rect":
                unsupported.append("nonrectangular_video")
            if (
                tag == "ln"
                and node.get("w") != "0"
                and not any(_local(child.tag) == "noFill" for child in node)
            ):
                unsupported.append("video_border")
            if tag == "lnRef" and node.get("idx", "0") != "0":
                unsupported.append("video_themed_border")
            if tag == "custGeom" or (tag in {"effectLst", "effectDag"} and len(node)):
                unsupported.append("video_shape_effect")
            if tag == "blip":
                poster = relationships.get(_attribute(node, "embed"))
                if poster and not poster["external"] and poster["type"] == "image":
                    record["poster_part"] = poster["target"]
        # Timing nodes are normal for media. Only unsupported playback modifiers
        # affecting this shape are flagged; the explicit notes supply scheduling.
        for node in slide.iter():
            if _local(node.tag) not in {"video", "audio"}:
                continue
            if not any(
                _local(child.tag) == "spTgt" and child.get("spid") == record["shape_id"]
                for child in node.iter()
            ):
                continue
            for child in node.iter():
                if any(child.get(key) not in {None, "1", "1000"} for key in ("repeatCount",)):
                    unsupported.append("video_repeat")
                if child.get("repeatDur") is not None:
                    unsupported.append("video_repeat")
                if child.get("spd") not in {None, "100000"}:
                    unsupported.append("video_speed")
                if child.get("autoRev", "0").lower() not in {"0", "false"}:
                    unsupported.append("video_reverse")
        if geometry:
            for later in shapes[z_order + 1 :]:
                other, changes = _geometry(later)
                if other is None or changes:
                    unsupported.append("possible_overlay_with_unknown_geometry")
                elif (
                    geometry["x_emu"] < other["x_emu"] + other["width_emu"]
                    and other["x_emu"] < geometry["x_emu"] + geometry["width_emu"]
                    and geometry["y_emu"] < other["y_emu"] + other["height_emu"]
                    and other["y_emu"] < geometry["y_emu"] + geometry["height_emu"]
                ):
                    unsupported.append("overlapping_foreground_shape")
        record["unsupported"] = sorted(set(unsupported))
        videos.append(record)
    if len(videos) > 1:
        for video in videos:
            video["unsupported"] = sorted(set(video["unsupported"] + ["multiple_videos_on_slide"]))
    return videos


def extract_video(source: str | Path, record: dict, outputpath: str | Path) -> dict:
    """Copy only an inventoried supported video, streaming and verifying its SHA."""
    if record.get("unsupported") or not record.get("part") or not record.get("sha256"):
        raise VPError("Embedded video requires unsupported-layout decisions", code="needs_decision")
    part = record["part"]
    if _resolve("", part) != part or not part.startswith("ppt/"):
        raise VPError("Invalid embedded video part")
    outputpath = Path(outputpath)
    if outputpath.resolve() == Path(source).resolve():
        raise VPError("Video extraction cannot overwrite the source presentation")
    outputpath.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(dir=outputpath.parent, suffix=".video-part")
    temporary = Path(temporary_name)
    try:
        digest = hashlib.sha256()
        total = 0
        with os.fdopen(fd, "wb") as target, ZipFile(source) as archive:
            if archive.getinfo(part).file_size > _MAX_VIDEO:
                raise VPError("Embedded video exceeds the 2 GiB supported size")
            with archive.open(part) as stream:
                while block := stream.read(1024 * 1024):
                    total += len(block)
                    if total > _MAX_VIDEO:
                        raise VPError("Embedded video exceeds the 2 GiB supported size")
                    digest.update(block)
                    target.write(block)
            target.flush()
            os.fsync(target.fileno())
        if digest.hexdigest() != record["sha256"] or total != record["size_bytes"]:
            raise VPError("Embedded video changed since inventory")
        os.replace(temporary, outputpath)
        return {"path": str(outputpath), "sha256": digest.hexdigest(), "size_bytes": total}
    except (KeyError, OSError, BadZipFile, RuntimeError) as exc:
        raise VPError(f"Cannot extract embedded video: {exc}") from exc
    finally:
        temporary.unlink(missing_ok=True)


def read_pptx(path: str | Path) -> dict:
    """Inventory every slide, including hidden and empty slides, in display order."""
    result = {"units": [], "slides": [], "issues": [], "presentation_size": None}
    unit_counter = 0
    try:
        with ZipFile(path) as archive:
            names = archive.namelist()
            if len(names) != len(set(names)):
                raise VPError("PPTX contains duplicate package part names")
            presentation_part = "ppt/presentation.xml"
            presentation = _xml(archive, presentation_part)
            dimensions = next((child for child in presentation if _local(child.tag) == "sldSz"), None)
            if dimensions is not None:
                try:
                    result["presentation_size"] = {
                        "width_emu": int(dimensions.attrib["cx"]),
                        "height_emu": int(dimensions.attrib["cy"]),
                    }
                    if min(result["presentation_size"].values()) <= 0:
                        raise ValueError("nonpositive slide size")
                except (KeyError, ValueError) as exc:
                    raise VPError("Invalid presentation slide dimensions") from exc
            content_types = _content_types(archive)
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
                slide_units = []
                for paragraph_index, source in enumerate(paragraphs, 1):
                    unit_counter += 1
                    unit_id = f"u{unit_counter:04d}"
                    slide_units.append(
                        {
                            "id": unit_id,
                            "source_text": source,
                            "slide_id": slide_id,
                            "paragraph_index": paragraph_index,
                        }
                    )
                timed = parse_timed_notes(slide_units)
                result["units"].extend(timed["units"])
                unit_ids = [unit["id"] for unit in timed["units"]]
                videos = _video_inventory(
                    archive, slide, slide_rels, content_types, result["presentation_size"]
                )
                has_notes = any(paragraph.strip() for paragraph in paragraphs)
                supported_video = len(videos) == 1 and not videos[0]["unsupported"]
                if any(video["unsupported"] for video in videos):
                    warnings.append("embedded_video_requires_decision")
                elif videos:
                    video_parts = {video["part"] for video in videos}
                    other_media = bool(tags & {"audioFile", "wavAudioFile"}) or any(
                        rel["type"] in {"audio", "video", "media"} and rel["target"] not in video_parts
                        for rel in slide_rels.values()
                    )
                    if not other_media and "embedded_media_not_preserved" in warnings:
                        warnings.remove("embedded_media_not_preserved")
                    if timed["timeline"] is None and has_notes:
                        warnings.append("embedded_video_requires_timed_notes")
                if not has_notes and not supported_video:
                    warnings.append("empty_notes_requires_silent_duration")
                result["slides"].append(
                    {
                        "id": slide_id,
                        "order": order,
                        "part": part,
                        "hidden": slide.get("show", "1").lower() in {"0", "false", "off"},
                        "unit_ids": unit_ids,
                        "warnings": warnings,
                        "videos": videos,
                        **({"timeline": timed["timeline"]} if timed["timeline"] else {}),
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
