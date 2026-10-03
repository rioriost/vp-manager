import json
import os
import sys
import time

import pytest
import soundfile as sf

from vp_manager import voicepeak
from vp_manager.common import VPError
from vp_manager.voicepeak import Voicepeak, engine_session


@pytest.fixture
def engine(tmp_path, monkeypatch):
    monkeypatch.setattr(voicepeak, "ensure_no_voicepeak", lambda: None)
    monkeypatch.setattr(voicepeak, "LOCK_PATH", tmp_path / "engine.lock")
    settings = tmp_path / "settings"
    settings.mkdir()
    (settings / "dic.json").write_text("[]")
    storage = tmp_path / "storage"
    storage.mkdir()
    (storage / "voice.sylapack").write_bytes(b"voice-v1")
    executable = tmp_path / "fake-engine"
    executable.write_text(f"#!{sys.executable}\n" + '''
import json, pathlib, sys, time
import numpy as np
import soundfile as sf
args = sys.argv[1:]
root = pathlib.Path(__file__).parent
with (root / 'calls.jsonl').open('a') as f: f.write(json.dumps(args) + '\\n')
if '--version' in args: print('1.2.23')
elif '--list-narrator' in args: print('Japanese Female 1')
elif '--list-emotion' in args: print('happy\\nsad')
elif '--say' in args:
    mode = (root / 'mode').read_text() if (root / 'mode').exists() else 'ok'
    if mode == 'timeout': time.sleep(10)
    if mode == 'failure':
        print('simulated failure', file=sys.stderr)
        sys.exit(3)
    output = pathlib.Path(args[args.index('--out') + 1])
    if mode == 'missing': sys.exit(0)
    if mode == 'broken': output.write_text('not audio'); sys.exit(0)
    sample = np.sin(np.arange(2400) * .1).astype(np.float32) * .2
    if mode == 'silent': sample *= 0
    sf.write(output, sample, 24000)
''')
    executable.chmod(0o700)
    return Voicepeak(executable, settings, timeout=3)


def test_inventory_and_valid_render(engine, tmp_path):
    inventory = engine.inventory()
    assert inventory["version"] == "1.2.23"
    assert inventory["narrators"] == ["Japanese Female 1"]
    result = engine.render("テストです。", tmp_path / "final.wav", "Japanese Female 1", emotion={"happy": 50})
    assert result["frames"] == 2400
    assert result["duration"] == .1
    assert sf.info(tmp_path / "final.wav").samplerate == 24000
    assert result["dictionary_hash"] == inventory["dictionary_hash"]
    assert not list(tmp_path.glob(".vp-render-*"))


@pytest.mark.parametrize("text", ["", " ", "あ" * 141, "abc\x00def", "<speak>hello</speak>", "C++を確認します"])
def test_invalid_input_never_launches_engine(engine, tmp_path, text):
    with pytest.raises(VPError):
        engine.render(text, tmp_path / "bad.wav", "Japanese Female 1")
    assert not (tmp_path / "calls.jsonl").exists()


def test_invalid_narrator_never_synthesizes(engine, tmp_path):
    with pytest.raises(VPError, match="Narrator"):
        engine.render("テスト", tmp_path / "bad.wav", "absent")
    calls = [json.loads(line) for line in (tmp_path / "calls.jsonl").read_text().splitlines()]
    assert all("--say" not in call for call in calls)


def test_unicode_boundary_counts_codepoints(engine, tmp_path):
    engine.render("あ" * 139 + "😀", tmp_path / "good.wav", "Japanese Female 1")


@pytest.mark.parametrize("mode", ["failure", "broken", "missing", "silent", "timeout"])
def test_failure_preserves_completed_output(engine, tmp_path, mode):
    engine.inventory()
    (tmp_path / "mode").write_text(mode)
    engine.timeout = .25 if mode == "timeout" else 3
    output = tmp_path / "final.wav"
    output.write_bytes(b"previous completed artifact")
    started = time.monotonic()
    with pytest.raises(VPError):
        engine.render("テスト", output, "Japanese Female 1")
    assert output.read_bytes() == b"previous completed artifact"
    assert time.monotonic() - started < 5
    assert not list(tmp_path.glob(".vp-render-*"))


def test_assets_and_dictionary_invalidate_identity_without_licensing_files(engine, tmp_path):
    first = engine.inventory()
    (tmp_path / "storage/license.idc").write_bytes(b"license irrelevant")
    assert engine.inventory()["voice_asset_fingerprint"] == first["voice_asset_fingerprint"]
    (tmp_path / "storage/voice.sylapack").write_bytes(b"voice-v2")
    second = engine.inventory()
    assert second["voice_asset_fingerprint"] != first["voice_asset_fingerprint"]
    (tmp_path / "settings/dic.json").write_text('[{"sur":"a"}]')
    assert engine.inventory()["dictionary_hash"] != first["dictionary_hash"]


def test_session_lock_is_exclusive_and_nested(engine):
    import fcntl
    with engine_session(), engine_session():
        fd = os.open(voicepeak.LOCK_PATH, os.O_RDWR)
        try:
            with pytest.raises(BlockingIOError):
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            os.close(fd)


def test_process_check_fails_closed(monkeypatch):
    import subprocess
    monkeypatch.setattr(voicepeak.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 0))
    with pytest.raises(VPError, match="Close"):
        voicepeak.ensure_no_voicepeak()


def test_installed_engine_cannot_claim_custom_settings(tmp_path):
    with pytest.raises(VPError, match="custom settings"):
        Voicepeak(settings=tmp_path)
