"""Explicit user-local Skill installation; importing/building never changes HOME.

Receipts establish local ownership, not a cryptographic publisher identity. Never
write Codex's cache: activation is delegated to the supported CLI.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
from importlib import resources
from pathlib import Path

from .common import VPError, atomic_json, file_lock

NAME = "voicepeak-production"
OWNER = "vp-manager.skill-installer.v1"
RECEIPT = ".vp-manager-install.json"


def _assets() -> dict[str, bytes]:
    root = resources.files("vp_manager").joinpath("bundled", NAME)
    if not root.is_dir():
        # Editable developer checkout only; installed wheels carry their own copy.
        checkout = Path(__file__).resolve().parents[2]
        if not (checkout / "pyproject.toml").is_file():
            raise VPError("Bundled Skill resources are missing; reinstall vp-manager", "environment")
        root = checkout / "plugins" / NAME
    result = {}

    def visit(directory, prefix=""):
        for item in directory.iterdir():
            relative = prefix + item.name
            if item.is_dir():
                visit(item, relative + "/")
            elif item.is_file():
                result[relative] = item.read_bytes()
    visit(root)
    if "plugin.json" not in result or f"skills/{NAME}/SKILL.md" not in result:
        raise VPError("Incomplete bundled Skill resources", "environment")
    return result


def _hashes(files):
    return {name: hashlib.sha256(data).hexdigest() for name, data in sorted(files.items())}


def _safe_path(path: Path):
    for node in (path, *path.parents):
        if node.is_symlink():
            raise VPError(f"Refusing symlink in installation path: {node}")


def _owned(path: Path) -> dict | None:
    _safe_path(path)
    if not path.exists():
        return None
    try:
        receipt = json.loads((path / RECEIPT).read_text())
        if receipt["owner"] != OWNER or receipt["schema_version"] != 1:
            raise ValueError("owner")
        files = {}
        directories = set()
        for item in path.rglob("*"):
            if item.is_symlink():
                raise ValueError("symlink")
            if item.is_dir():
                directories.add(str(item.relative_to(path)))
            if item.is_file() and item != path / RECEIPT:
                files[str(item.relative_to(path))] = item.read_bytes()
        expected_directories = {str(parent) for name in files for parent in Path(name).parents
                                if parent != Path(".")}
        if directories != expected_directories or _hashes(files) != receipt["files"]:
            raise ValueError("changed content")
        return receipt
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise VPError(f"Preserving unowned or edited Skill directory: {path}") from exc


def _sync_dir(path):
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _deploy(path: Path, files: dict[str, bytes]):
    """Recover a verified previous generation, then atomically replace it."""
    backup = path.with_name(path.name + ".vp-manager-backup")
    current, old = _owned(path), _owned(backup)
    if old is not None:
        if current is None:
            os.replace(backup, path)
            current = old
        else:
            _owned(backup)
            shutil.rmtree(backup)
        _sync_dir(path.parent)
    if current and current["files"] == _hashes(files):
        return
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    # Unique staging names let retries proceed even after an incomplete copy.
    # Orphan stages are never mistaken for a committed generation or deleted.
    staging = Path(tempfile.mkdtemp(prefix=".vp-manager-stage-", dir=path.parent))
    for relative, data in files.items():
        target = staging / relative
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with target.open("xb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
    atomic_json(staging / RECEIPT, {"schema_version": 1, "owner": OWNER, "files": _hashes(files)})
    for directory in sorted((p for p in staging.rglob("*") if p.is_dir()),
                            key=lambda p: len(p.parts), reverse=True):
        _sync_dir(directory)
    _sync_dir(staging)
    if _owned(path) != current:
        raise VPError(f"Skill changed during installation; preserving {path} and {staging}")
    if current:
        os.replace(path, backup)
    os.replace(staging, path)
    _sync_dir(path.parent)
    if backup.exists():
        _owned(backup)
        shutil.rmtree(backup)
        _sync_dir(path.parent)


def _discover_codex(home: Path) -> Path | None:
    command = shutil.which("codex")
    candidates = [Path(command)] if command else []
    for base in (home / "Applications", Path("/Applications")):
        for app in ("ChatGPT.app", "Codex.app"):
            for binary in ("codex-cli", "codex"):
                candidates.append(base / app / "Contents/Resources" / binary)
    return next((p for p in candidates if p.is_file() and os.access(p, os.X_OK)), None)


def _catalog(path: Path, source: str, plugin: Path) -> dict:
    _safe_path(path)
    try:
        data = json.loads(path.read_text()) if path.exists() else {
            "name": "vp-manager-local", "interface": {"displayName": "vp-manager Local"}, "plugins": []
        }
        if not isinstance(data.get("name"), str) or not data["name"] or not isinstance(data["plugins"], list):
            raise ValueError("marketplace shape")
        matching = [entry for entry in data["plugins"] if entry.get("name") == NAME]
        if len(matching) > 1:
            raise ValueError("duplicate plugin")
        if matching:
            if matching[0].get("source") != {"source": "local", "path": source}:
                raise VPError(f"Preserving existing marketplace entry for {NAME}: {path}")
            # A matching path alone is insufficient to claim an existing entry.
            if _owned(plugin) is None and _owned(plugin.with_name(plugin.name + ".vp-manager-backup")) is None:
                raise VPError(f"Existing marketplace entry has no owned source: {path}")
        else:
            data["plugins"].append({
                "name": NAME, "source": {"source": "local", "path": source},
                "policy": {"installation": "AVAILABLE", "authentication": "ON_INSTALL"},
                "category": "Productivity",
            })
        return data
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        raise VPError(f"Invalid personal marketplace: {path}") from exc


def install_skill(*, home: Path | None = None, codex_executable: Path | None = None,
                  dry_run: bool = False) -> dict:
    """Install copied assets and activate officially, or install standalone fallback.

    ``home`` is an isolation seam for tests. No commands or writes occur in dry
    run. An unavailable/unsupported Codex CLI uses a self-contained standalone
    Skill; a failed supported plugin add leaves registration pending explicitly.
    """
    home = Path(home).absolute() if home is not None else Path.home()
    plugin = home / ".codex/plugins/vp-manager" / NAME
    standalone = home / ".codex/skills" / NAME
    marketplace = home / ".agents/plugins/marketplace.json"
    source = f"./.codex/plugins/vp-manager/{NAME}"
    files = _assets()
    executable = Path(codex_executable) if codex_executable is not None else _discover_codex(home)
    result = {"status": "planned", "plugin_path": str(plugin), "marketplace_path": str(marketplace),
              "codex_executable": str(executable) if executable else None, "restart_required": True,
              "dry_run": dry_run}
    # Preflight before any directories/lock are created; check orphan generations too.
    for destination in (plugin, standalone):
        for candidate in (destination, destination.with_name(destination.name + ".vp-manager-backup")):
            _owned(candidate)
    _catalog(marketplace, source, plugin)
    if dry_run:
        return {**result, "method": "codex_plugin_if_supported_else_standalone" if executable else "standalone"}
    env = {**os.environ, "HOME": str(home), "CODEX_HOME": str(home / ".codex")}
    supported = False
    if executable:
        try:
            probe = subprocess.run([str(executable), "plugin", "add", "--help"], env=env,
                                   capture_output=True, text=True, timeout=15, check=False)
            supported = probe.returncode == 0 and "--marketplace" in probe.stdout and "--json" in probe.stdout
        except (OSError, subprocess.TimeoutExpired):
            pass
    lock_path = home / ".codex/vp-manager-skill-install.lock"
    _safe_path(lock_path)
    with file_lock(lock_path):
        catalog = _catalog(marketplace, source, plugin)
        if supported:
            _deploy(plugin, files)
            # Another marketplace writer does not share our lock. Merge its latest
            # entries after copying rather than publishing our pre-copy snapshot.
            catalog = _catalog(marketplace, source, plugin)
            atomic_json(marketplace, catalog)
            result.update(method="codex_plugin", marketplace=catalog["name"])
            try:
                activation = subprocess.run(
                    [str(executable), "plugin", "add", NAME, "--marketplace", catalog["name"], "--json"],
                    env=env, capture_output=True, text=True, timeout=60, check=False)
                if activation.returncode != 0:
                    raise VPError(activation.stderr.strip() or activation.stdout.strip() or "plugin add failed")
                payload = json.loads(activation.stdout)
                if not isinstance(payload, dict) or payload.get("name") != NAME:
                    raise VPError("Codex plugin add returned an unexpected identity; inspect plugin list")
                result.update(status="installed", activation=payload)
            except (OSError, subprocess.TimeoutExpired, ValueError, VPError) as exc:
                result.update(status="pending_activation", reason=str(exc),
                              next_action="Run codex plugin add voicepeak-production --marketplace "
                              + catalog["name"] + " --json, then restart the host.")
        else:
            prefix = f"skills/{NAME}/"
            skill_files = {name.removeprefix(prefix): data for name, data in files.items() if name.startswith(prefix)}
            _deploy(standalone, skill_files)
            result.update(status="installed", method="standalone", skill_path=str(standalone),
                          next_action="Restart the host to discover the installed standalone Skill.")
    return result
