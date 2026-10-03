"""Release helper checks use synthetic lock data and disposable wheels only."""
from __future__ import annotations

import importlib.util
import json
import zipfile
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location(
    "prepare_homebrew_resources", Path(__file__).parents[1] / "scripts/prepare_homebrew_resources.py"
)
helper = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(helper)


def wheel(name, version="1.0", tags="py3-none-any", host="files.pythonhosted.org"):
    return {
        "url": f"https://{host}/packages/ab/{name.replace('-', '_')}-{version}-{tags}.whl",
        "hash": "sha256:" + "1" * 64, "size": 123,
    }


def package(name, **kwargs):
    return {"name": name, "version": "1.0", "wheels": [wheel(name)], **kwargs}


def lock(*packages, core=None, asr=None):
    return {"package": [
        {"name": "vp-manager", "version": "0.1.0", "dependencies": core or [],
         "optional-dependencies": {"asr": asr or []},
         "dev-dependencies": {"dev": [{"name": "pymupdf"}]}},
        *packages,
    ]}


def test_runtime_closure_target_markers_and_transitive_asr():
    model = package("mlx-whisper", dependencies=[{"name": "mlx"}, {"name": "numpy"}])
    data = lock(package("numpy"), model, package("mlx"), package("pymupdf"), package("colorama"),
                core=[{"name": "numpy"}, {"name": "colorama", "marker": "sys_platform == 'win32'"}],
                asr=[{"name": "mlx-whisper", "marker": "sys_platform == 'darwin'"}])
    assert [item["name"] for item in helper.select_resources(data)] == ["mlx", "mlx-whisper", "numpy"]


def test_native_wheel_target_is_sonoma_even_on_newer_host():
    native = package("mlx", wheels=[
        wheel("mlx", tags="cp312-cp312-macosx_26_0_arm64"),
        wheel("mlx", tags="cp312-cp312-macosx_14_0_arm64"),
        wheel("mlx", tags="cp312-cp312-macosx_14_0_x86_64"),
    ])
    selected = helper.select_resources(lock(native, core=[{"name": "mlx"}]))
    assert selected[0]["filename"].endswith("macosx_14_0_arm64.whl")


def test_pure_soundfile_uses_external_native_library():
    soundfile = package("soundfile", wheels=[
        wheel("soundfile", tags="py2.py3-none-macosx_11_0_arm64"), wheel("soundfile"),
    ])
    selected = helper.select_resources(lock(soundfile, core=[{"name": "soundfile"}]))
    assert selected[0]["filename"] == "soundfile-1.0-py3-none-any.whl"


@pytest.mark.parametrize("bad", [
    package("example", wheels=[]),
    package("example", wheels=[wheel("example", tags="cp313-cp313-macosx_14_0_arm64")]),
    package("example", wheels=[wheel("example", host="private.example")]),
    package("example", wheels=[{**wheel("example"), "hash": "sha256:short"}]),
    package("example", wheels=[wheel("different-package")]),
])
def test_reject_unreproducible_or_wrong_artifacts(bad):
    with pytest.raises(ValueError):
        helper.select_resources(lock(bad, core=[{"name": "example"}]))


def test_ambiguous_lock_versions_fail_closed():
    with pytest.raises(ValueError, match="Ambiguous"):
        helper.select_resources(lock(package("numpy"), package("numpy", version="2.0")))


def test_extra_dependencies_propagate():
    library = package("library", **{"optional-dependencies": {"feature": [{"name": "nested"}]}})
    data = lock(library, package("nested"), core=[{"name": "library", "extra": ["feature"]}])
    assert [item["name"] for item in helper.select_resources(data)] == ["library", "nested"]


def make_release(path, include_skill=True):
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("vp_manager-0.1.0.dist-info/METADATA", "Name: vp-manager\nVersion: 0.1.0\n")
        if include_skill:
            archive.writestr(
                "vp_manager/bundled/voicepeak-production/skills/voicepeak-production/SKILL.md", "test skill"
            )


def test_release_sha_matches_exact_packaged_skill_wheel(tmp_path):
    path = tmp_path / "vp_manager-0.1.0-py3-none-any.whl"
    make_release(path)
    release = helper.release_wheel(path, "0.1.0")
    assert release["sha256"] == helper.digest(path)
    assert release["url"].endswith("/releases/download/v0.1.0/" + path.name)
    make_release(path, include_skill=False)
    with pytest.raises(ValueError, match="Skill"):
        helper.release_wheel(path, "0.1.0")


def test_existing_prefetch_hash_mismatch_preserved(tmp_path):
    resource = helper.select_resources(lock(package("numpy"), core=[{"name": "numpy"}]))[0]
    target = tmp_path / resource["filename"]
    target.write_bytes(b"external file")
    with pytest.raises(ValueError, match="hash mismatch"):
        helper.download_resources([resource], tmp_path)
    assert target.read_bytes() == b"external file"


def test_generation_is_deterministic_and_uses_immutable_checksums(tmp_path):
    path = tmp_path / "vp_manager-0.1.0-py3-none-any.whl"
    make_release(path)
    project = helper.release_wheel(path, "0.1.0")
    resources = helper.select_resources(lock(package("numpy"), core=[{"name": "numpy"}]))
    template = '@@URL@@\n@@SHA256@@\n@@VERSION@@\n@@RESOURCES@@\n'
    first = helper.render_formula(template, project, resources)
    assert first == helper.render_formula(template, project, json.loads(json.dumps(resources)))
    assert project["sha256"] in first
    assert resources[0]["sha256"] in first
    assert "@@" not in first
