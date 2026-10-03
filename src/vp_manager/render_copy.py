"""Prepare a separate all-slide copy for an external presentation renderer."""

from __future__ import annotations

import os
import re
import shutil
import tempfile
from copy import copy
from pathlib import Path
from xml.parsers import expat
from zipfile import ZipFile

from .common import VPError, atomic_json, sha256
from .pptx import read_pptx


def _show_slide(raw: bytes) -> bytes:
    """Change only the actual root's unqualified visibility attribute value."""
    parser = expat.ParserCreate(namespace_separator="}")
    root_start = None

    class RootFound(Exception):
        pass

    def root(name, attributes):
        nonlocal root_start
        if name.rsplit("}", 1)[-1] != "sld" or attributes.get("show", "").lower() not in {"0", "false", "off"}:
            raise VPError("Cannot locate the hidden slide root visibility attribute")
        root_start = parser.CurrentByteIndex
        raise RootFound

    parser.StartElementHandler = root
    try:
        parser.Parse(raw, True)
    except RootFound:
        pass
    except expat.ExpatError as exc:
        raise VPError("Cannot parse slide visibility attribute") from exc
    if root_start is None:
        raise VPError("Cannot locate slide root")
    # Quoted > characters and fake tags in XML comments do not terminate or
    # redirect this opening-tag match. Non-ASCII-compatible XML fails closed.
    opening = re.match(rb'''<(?:[^<>"']|"[^"]*"|'[^']*')*>''', raw[root_start:])
    if opening is None or b"\x00" in opening.group():
        raise VPError("Hidden slide visibility editing requires ASCII-compatible XML encoding")
    attribute = re.compile(rb'''\s+([^\s=/>]+)\s*=\s*(['"])(.*?)\2''', re.DOTALL)
    matches = [match for match in attribute.finditer(opening.group()) if match.group(1) == b"show"]
    if len(matches) != 1:
        raise VPError("Ambiguous slide visibility attribute")
    match = matches[0]
    start, end = root_start + match.start(3), root_start + match.end(3)
    return raw[:start] + b"1" + raw[end:]


def _publish(temporary: Path, destination: Path) -> None:
    """Publish on the same filesystem without replacing a racing writer."""
    try:
        os.link(temporary, destination)
    except FileExistsError as exc:
        raise VPError(f"Rendering-copy destination already exists: {destination}") from exc
    directory = os.open(destination.parent, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def prepare(source: Path, destination: Path) -> dict:
    source = Path(source).resolve()
    destination = Path(destination).absolute()
    if destination.is_symlink():
        raise VPError("Rendering copy requires a new destination, not a symlink")
    destination = destination.resolve()
    provenance = destination.with_suffix(".provenance.json")
    if source == destination or destination.exists() or provenance.exists() or provenance.is_symlink() or provenance == destination:
        raise VPError("Rendering copy requires a new destination; the original is never overwritten")
    original_hash = sha256(source)
    inventory = read_pptx(source)
    hidden = {s["part"]: s for s in inventory["slides"] if s["hidden"]}
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(prefix=".render-copy-", suffix=".pptx", dir=destination.parent)
    os.close(fd)
    temporary = Path(temporary)
    metadata_temporary = None
    changed = set()
    try:
        with ZipFile(source) as original, ZipFile(temporary, "w") as output:
            output.comment = original.comment
            for info in original.infolist():
                output_info = copy(info)
                if info.filename in hidden:
                    raw = _show_slide(original.read(info))
                    output.writestr(output_info, raw)
                    changed.add(hidden[info.filename]["id"])
                else:
                    with original.open(info) as source_member, output.open(output_info, "w") as copy_member:
                        shutil.copyfileobj(source_member, copy_member)
        result_inventory = read_pptx(temporary)
        if (
            any(s["hidden"] for s in result_inventory["slides"])
            or result_inventory["units"] != inventory["units"]
        ):
            raise VPError("Rendering copy did not preserve notes and expose all slides")
        if sha256(source) != original_hash:
            raise VPError("Source changed during rendering-copy preparation", code="needs_recovery")
        with temporary.open("rb") as stream:
            os.fsync(stream.fileno())
        result = {
            "schema_version": 1,
            "source_sha256": original_hash,
            "render_copy": str(destination),
            "render_copy_sha256": sha256(temporary),
            "slide_ids": [s["id"] for s in inventory["slides"]],
            "temporarily_visible": [slide["id"] for slide in inventory["slides"] if slide["id"] in changed],
            "original_unchanged": True,
            "notes_unchanged": True,
            "next_action": "Export this copy locally from PowerPoint as PDF; retain every slide in order",
        }
        fd, metadata_name = tempfile.mkstemp(prefix=".render-copy-", suffix=".json", dir=destination.parent)
        os.close(fd)
        metadata_temporary = Path(metadata_name)
        atomic_json(metadata_temporary, result)
        _publish(temporary, destination)
        try:
            _publish(metadata_temporary, provenance)
        except VPError as exc:
            raise VPError(f"Rendering copy published at {destination}, but provenance was not published: {exc}") from exc
        return result
    finally:
        temporary.unlink(missing_ok=True)
        if metadata_temporary is not None:
            metadata_temporary.unlink(missing_ok=True)
