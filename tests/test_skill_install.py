import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from vp_manager import skill_install as installer
from vp_manager.common import VPError


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setattr(installer, "_discover_codex", lambda home: None)
    return tmp_path / "user with spaces"


def fake_cli(monkeypatch, *, failure=False, hook=None):
    calls = []

    def run(args, **kwargs):
        calls.append((args, kwargs))
        if args[-1] == "--help":
            return SimpleNamespace(returncode=0, stdout="--marketplace --json", stderr="")
        if hook:
            hook()
        return SimpleNamespace(returncode=1 if failure else 0,
                               stdout=json.dumps({"name": installer.NAME}), stderr="failed" if failure else "")
    monkeypatch.setattr(installer.subprocess, "run", run)
    return calls


def test_dry_run_does_not_write_or_probe(home, monkeypatch):
    monkeypatch.setattr(installer.subprocess, "run", lambda *a, **k: pytest.fail("unexpected subprocess"))
    result = installer.install_skill(home=home, codex_executable=Path("/fake/codex"), dry_run=True)
    assert result["status"] == "planned"
    assert not home.exists()


def test_standalone_copied_complete_and_repeatable(home):
    result = installer.install_skill(home=home)
    skill = Path(result["skill_path"])
    assert result["method"] == "standalone"
    assert (skill / "LICENSE").is_file()
    text = (skill / "SKILL.md").read_text()
    assert "VP_MANAGER=(vp-manager)" in text and "../../../docs" not in text
    assert (skill / "references/references.md").is_file()
    receipt = (skill / installer.RECEIPT).read_bytes()
    assert installer.install_skill(home=home)["status"] == "installed"
    assert (skill / installer.RECEIPT).read_bytes() == receipt
    assert not (home / ".agents").exists()


def test_plugin_preserves_marketplace_and_uses_supported_cli(home, monkeypatch):
    catalog = home / ".agents/plugins/marketplace.json"
    catalog.parent.mkdir(parents=True)
    other = {"name": "other", "source": {"source": "local", "path": "./other"}, "custom": 12}
    catalog.write_text(json.dumps({"name": "my-personal", "custom": True, "plugins": [other]}))
    calls = fake_cli(monkeypatch)
    result = installer.install_skill(home=home, codex_executable=Path("/fake/codex"))
    assert result["status"] == "installed" and result["method"] == "codex_plugin"
    data = json.loads(catalog.read_text())
    assert data["custom"] is True and data["plugins"][0] == other
    assert data["plugins"][1]["source"]["path"] == "./.codex/plugins/vp-manager/voicepeak-production"
    assert calls[1][0] == ["/fake/codex", "plugin", "add", installer.NAME, "--marketplace", "my-personal", "--json"]
    assert all(call[1]["env"]["HOME"] == str(home) for call in calls)
    assert not (home / ".codex/plugins/cache").exists()


def test_cli_failure_reports_pending_not_success(home, monkeypatch):
    fake_cli(monkeypatch, failure=True)
    result = installer.install_skill(home=home, codex_executable=Path("/fake/codex"))
    assert result["status"] == "pending_activation"
    assert Path(result["plugin_path"]).is_dir()


def test_unsupported_cli_installs_standalone(home, monkeypatch):
    monkeypatch.setattr(installer.subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=2, stdout=""))
    assert installer.install_skill(home=home, codex_executable=Path("/fake"))["method"] == "standalone"


@pytest.mark.parametrize("edited", [False, True])
def test_foreign_and_edited_skills_preserved(home, edited):
    path = home / ".codex/skills" / installer.NAME
    if edited:
        installer.install_skill(home=home)
    else:
        path.mkdir(parents=True)
    file = path / "SKILL.md"
    file.write_text("user-owned text")
    with pytest.raises(VPError, match="unowned or edited"):
        installer.install_skill(home=home)
    assert file.read_text() == "user-owned text"


def test_legacy_same_name_marketplace_not_overwritten(home):
    catalog = home / ".agents/plugins/marketplace.json"
    catalog.parent.mkdir(parents=True)
    content = json.dumps({"name": "legacy", "plugins": [{"name": installer.NAME, "source": {
        "source": "local", "path": "./Git_Managed/vp-manager/plugins/voicepeak-production"}}]})
    catalog.write_text(content)
    with pytest.raises(VPError, match="existing marketplace"):
        installer.install_skill(home=home)
    assert catalog.read_text() == content
    assert not (home / ".codex").exists()


def test_managed_upgrade_and_interrupted_swap_retry(home, monkeypatch):
    result = installer.install_skill(home=home)
    skill = Path(result["skill_path"])
    files = installer._assets()
    files[f"skills/{installer.NAME}/SKILL.md"] += b"\nupdated\n"
    monkeypatch.setattr(installer, "_assets", lambda: files)
    original = installer.os.replace
    interrupted = False

    def replace(source, dest):
        nonlocal interrupted
        if Path(dest) == skill and ".vp-manager-stage-" in Path(source).name and not interrupted:
            interrupted = True
            raise OSError("simulated interruption after backup")
        return original(source, dest)
    monkeypatch.setattr(installer.os, "replace", replace)
    with pytest.raises(OSError, match="interruption"):
        installer.install_skill(home=home)
    assert not skill.exists()
    assert skill.with_name(skill.name + ".vp-manager-backup").is_dir()
    assert installer.install_skill(home=home)["status"] == "installed"
    assert (skill / "SKILL.md").read_bytes().endswith(b"updated\n")
    assert not skill.with_name(skill.name + ".vp-manager-backup").exists()


def test_external_edit_during_staging_is_preserved(home, monkeypatch):
    result = installer.install_skill(home=home)
    skill = Path(result["skill_path"])
    files = installer._assets()
    files[f"skills/{installer.NAME}/SKILL.md"] += b"\nupdated\n"
    monkeypatch.setattr(installer, "_assets", lambda: files)
    atomic = installer.atomic_json

    def edit(path, value):
        atomic(path, value)
        if path.name == installer.RECEIPT:
            (skill / "SKILL.md").write_text("concurrent user edit")
    monkeypatch.setattr(installer, "atomic_json", edit)
    with pytest.raises(VPError, match="edited"):
        installer.install_skill(home=home)
    assert (skill / "SKILL.md").read_text() == "concurrent user edit"


def test_new_marketplace_entry_during_copy_survives(home, monkeypatch):
    fake_cli(monkeypatch)
    deploy = installer._deploy
    catalog = home / ".agents/plugins/marketplace.json"

    def concurrent(path, files):
        deploy(path, files)
        catalog.parent.mkdir(parents=True, exist_ok=True)
        catalog.write_text(json.dumps({"name": "other-writer", "plugins": [{"name": "kept"}]}))
    monkeypatch.setattr(installer, "_deploy", concurrent)
    installer.install_skill(home=home, codex_executable=Path("/fake"))
    assert json.loads(catalog.read_text())["plugins"][0] == {"name": "kept"}


def test_symlink_destination_rejected(home, tmp_path):
    target = tmp_path / "existing"
    target.mkdir()
    home.mkdir()
    (home / ".codex").symlink_to(target, target_is_directory=True)
    with pytest.raises(VPError, match="symlink"):
        installer.install_skill(home=home)
    assert list(target.iterdir()) == []


def test_app_cli_discovered_without_running_it(tmp_path, monkeypatch):
    monkeypatch.setattr(installer.shutil, "which", lambda command: None)
    executable = tmp_path / "Applications/ChatGPT.app/Contents/Resources/codex-cli"
    executable.parent.mkdir(parents=True)
    executable.write_text("fake")
    executable.chmod(0o700)
    assert installer._discover_codex(tmp_path) == executable


@pytest.mark.parametrize("arguments", [["--help"], ["--version"], ["install-skill", "--help"], ["status", "--help"]])
def test_cli_information_never_constructs_engine_or_runs_subprocess(arguments, monkeypatch):
    from vp_manager import cli

    def forbidden(*args, **kwargs):
        pytest.fail("information command attempted engine/subprocess")
    monkeypatch.setattr(cli, "Voicepeak", forbidden)
    monkeypatch.setattr(installer.subprocess, "run", forbidden)
    with pytest.raises(SystemExit) as exit_info:
        cli.main(arguments)
    assert exit_info.value.code == 0


@pytest.mark.parametrize("status, expected", [("planned", 0), ("installed", 0), ("pending_activation", 3)])
def test_cli_install_json_and_exit_status(status, expected, monkeypatch, capsys):
    from vp_manager import cli

    calls = []

    def install(**kwargs):
        calls.append(kwargs)
        return {"status": status, "method": "codex_plugin"}
    monkeypatch.setattr(installer, "install_skill", install)
    monkeypatch.setattr(cli, "Voicepeak", lambda *a, **k: pytest.fail("engine must not be constructed"))
    assert cli.main(["install-skill", "--dry-run", "--json"]) == expected
    assert calls == [{"dry_run": True}]
    assert json.loads(capsys.readouterr().out)["status"] == status
