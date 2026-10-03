#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = ["packaging==26.0"]
# ///
"""Generate the arm64 Homebrew formula from the public lock and release wheel.

Run with the project's Python (packaging required), or ``uv run --script``.
Resolution never happens here: the existing uv.lock supplies every version and
hash. Optional downloads verify those hashes and prepare an offline wheelhouse.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import tempfile
import tomllib
import urllib.parse
import urllib.request
import zipfile
from email.parser import BytesParser
from pathlib import Path

from packaging.markers import Marker, default_environment
from packaging.tags import compatible_tags, cpython_tags, mac_platforms
from packaging.utils import canonicalize_name, parse_wheel_filename

ROOT = Path(__file__).resolve().parents[1]
TARGET = {"python": "3.12", "os": "macos", "minimum_macos": "14.0", "arch": "arm64"}


def digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def marker_environment() -> dict[str, str]:
    return {
        **default_environment(),
        "implementation_name": "cpython",
        "implementation_version": "3.12.0",
        "os_name": "posix",
        "platform_machine": "arm64",
        "platform_python_implementation": "CPython",
        "platform_release": "23.0.0",
        "platform_system": "Darwin",
        "platform_version": "14.0",
        "python_full_version": "3.12.0",
        "python_version": "3.12",
        "sys_platform": "darwin",
        "extra": "",
    }


def dependency_packages(lock: dict) -> list[dict]:
    """Walk runtime plus ASR closure; dev-only PDF tooling stays excluded."""
    packages: dict[str, dict] = {}
    for package in lock["package"]:
        name = canonicalize_name(package["name"])
        if name in packages:
            raise ValueError(f"Ambiguous locked package {name}; resolve lock forks explicitly")
        packages[name] = package
    selected: set[str] = set()
    visited: set[tuple[str, tuple[str, ...]]] = set()
    queue = [("vp-manager", ("asr",))]
    environment = marker_environment()
    while queue:
        name, extras = queue.pop()
        key = (name, extras)
        if key in visited:
            continue
        visited.add(key)
        package = packages[name]
        if name != "vp-manager":
            selected.add(name)
        edges = list(package.get("dependencies", []))
        for extra in extras:
            edges.extend(package.get("optional-dependencies", {}).get(extra, []))
        for edge in edges:
            marker = edge.get("marker")
            if marker and not any(
                Marker(marker).evaluate({**environment, "extra": extra}) for extra in ("", *extras)
            ):
                continue
            queue.append((canonicalize_name(edge["name"]), tuple(sorted(edge.get("extra", [])))))
    return [packages[name] for name in sorted(selected)]


def select_resources(lock: dict) -> list[dict]:
    platforms = list(mac_platforms(version=(14, 0), arch="arm64"))
    supported = list(cpython_tags(python_version=(3, 12), platforms=platforms))
    supported += list(compatible_tags(python_version=(3, 12), interpreter="cp312", platforms=platforms))
    ranks = {tag: rank for rank, tag in enumerate(supported)}
    resources = []
    for package in dependency_packages(lock):
        candidates = []
        for wheel in package.get("wheels", []):
            url = urllib.parse.urlsplit(wheel["url"])
            filename = urllib.parse.unquote(Path(url.path).name)
            name, version, _, tags = parse_wheel_filename(filename)
            compatible = tags.intersection(ranks)
            if not compatible:
                continue
            if canonicalize_name(package["name"]) != name or str(version) != package["version"]:
                raise ValueError(f"Wheel identity differs from locked package: {filename}")
            # Prefer pure wheels when offered. In particular, soundfile then uses
            # Homebrew libsndfile instead of carrying a second native library.
            pure = any(tag.abi == "none" and tag.platform == "any" for tag in compatible)
            candidates.append((not pure, min(ranks[tag] for tag in compatible), filename, wheel))
        if not candidates:
            raise ValueError(f"No CPython 3.12/macOS 14 arm64 wheel for {package['name']}")
        _, _, filename, wheel = min(candidates, key=lambda item: item[:3])
        url = urllib.parse.urlsplit(wheel["url"])
        if url.scheme != "https" or url.netloc != "files.pythonhosted.org" or url.query or url.fragment:
            raise ValueError(f"Public immutable PyPI wheel required: {wheel['url']}")
        checksum = wheel["hash"]
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", checksum):
            raise ValueError(f"Invalid locked SHA-256 for {filename}")
        resources.append({
            "name": package["name"], "version": package["version"], "filename": filename,
            "url": wheel["url"], "sha256": checksum.removeprefix("sha256:"),
            "size": wheel.get("size"),
        })
    return resources


def release_wheel(path: Path, version: str) -> dict:
    expected = f"vp_manager-{version}-py3-none-any.whl"
    if path.name != expected:
        raise ValueError(f"Expected release wheel {expected}")
    with zipfile.ZipFile(path) as archive:
        metadata = BytesParser().parsebytes(archive.read(f"vp_manager-{version}.dist-info/METADATA"))
        if metadata["Name"] != "vp-manager" or metadata["Version"] != version:
            raise ValueError("Release wheel metadata does not match locked project version")
        skill = "vp_manager/bundled/voicepeak-production/skills/voicepeak-production/SKILL.md"
        if skill not in archive.namelist():
            raise ValueError("Release wheel is missing the bundled Skill")
    return {
        "name": "vp-manager", "version": version, "filename": expected,
        "url": f"https://github.com/rioriost/vp-manager/releases/download/v{version}/{expected}",
        "sha256": digest(path), "size": path.stat().st_size,
    }


def render_formula(template: str, project: dict, resources: list[dict]) -> str:
    blocks = []
    for resource in resources:
        # PyYAML's pinned binary wheel embeds libyaml; no source compilation or
        # dynamic use of Homebrew libyaml occurs during this wheel-only install.
        exemption = (
            "  # The pinned PyYAML binary wheel embeds libyaml; no external libyaml is used.\n"
            if resource["name"] == "pyyaml" else ""
        )
        blocks.append(
            exemption + f'  resource "{resource["name"]}" do\n'
            f'    url "{resource["url"]}", using: :nounzip\n'
            f'    sha256 "{resource["sha256"]}"\n'
            "  end"
        )
    replacements = {
        "@@VERSION@@": project["version"], "@@URL@@": project["url"],
        "@@SHA256@@": project["sha256"], "@@RESOURCES@@": "\n\n".join(blocks),
    }
    for key, value in replacements.items():
        template = template.replace(key, value)
    if "@@" in template:
        raise ValueError("Unresolved formula template substitution")
    return template


def download_resources(resources: list[dict], directory: Path) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    for resource in resources:
        target = directory / resource["filename"]
        if target.exists():
            if digest(target) != resource["sha256"]:
                raise ValueError(f"Existing wheel hash mismatch: {target}")
            continue
        with tempfile.NamedTemporaryFile(dir=directory, suffix=".part", delete=False) as stream:
            temporary = Path(stream.name)
            try:
                with urllib.request.urlopen(resource["url"], timeout=60) as response:
                    if urllib.parse.urlsplit(response.url).netloc != "files.pythonhosted.org":
                        raise ValueError("Unexpected wheel download redirect")
                    shutil.copyfileobj(response, stream)
                stream.flush()
                if digest(temporary) != resource["sha256"]:
                    raise ValueError(f"Downloaded wheel hash mismatch: {target}")
                temporary.replace(target)
            finally:
                temporary.unlink(missing_ok=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lock", type=Path, default=ROOT / "uv.lock")
    parser.add_argument("--wheel", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, default=ROOT / "packaging/homebrew")
    parser.add_argument("--download-dir", type=Path, help="Optionally prefetch and verify dependency wheels")
    parser.add_argument("--check", action="store_true", help="Require generated files to match; do not write")
    args = parser.parse_args()
    lock = tomllib.loads(args.lock.read_text())
    version = next(package["version"] for package in lock["package"] if package["name"] == "vp-manager")
    project = release_wheel(args.wheel, version)
    resources = select_resources(lock)
    manifest = {
        "schema_version": 1, "target": TARGET, "lock_sha256": digest(args.lock),
        "project": project, "resources": resources,
    }
    template = (ROOT / "packaging/homebrew/vp-manager.rb.in").read_text()
    generated = {
        "vp-manager.rb": render_formula(template, project, resources),
        "resources-macos-arm64.json": json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
    }
    for filename, content in generated.items():
        path = args.output_dir / filename
        if args.check:
            if not path.is_file() or path.read_text() != content:
                raise SystemExit(f"Out of date: {path}")
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
    if args.download_dir:
        download_resources(resources, args.download_dir)
        target = args.download_dir / args.wheel.name
        if target.exists() and digest(target) != project["sha256"]:
            raise ValueError(f"Existing release wheel hash mismatch: {target}")
        if args.wheel.resolve() != target.resolve():
            shutil.copyfile(args.wheel, target)
    print(json.dumps({"resources": len(resources), "dependency_bytes": sum(r["size"] or 0 for r in resources),
                      "project_sha256": project["sha256"], "target": TARGET}))


if __name__ == "__main__":
    main()
