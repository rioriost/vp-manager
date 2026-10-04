import hashlib
import json
import os
import signal
import subprocess
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
import json, os, pathlib, signal, stat, subprocess, sys, time
import numpy as np
import soundfile as sf
args = sys.argv[1:]
root = pathlib.Path(__file__).parent
with (root / 'calls.jsonl').open('a') as f: f.write(json.dumps(args) + '\\n')
if '--version' in args: print('1.2.23')
elif '--list-narrator' in args: print('Japanese Female 1')
elif '--list-emotion' in args: print('happy\\nsad')
elif '--text' in args:
    input_file = pathlib.Path(args[args.index('--text') + 1])
    (root / 'observed.json').write_text(json.dumps({
        'args': args, 'cwd': str(pathlib.Path.cwd()), 'text_hex': input_file.read_bytes().hex(),
        'file_mode': stat.S_IMODE(input_file.stat().st_mode),
        'directory_mode': stat.S_IMODE(pathlib.Path.cwd().stat().st_mode),
    }))
    mode = (root / 'mode').read_text() if (root / 'mode').exists() else 'ok'
    if mode in ('child_failure', 'child_timeout', 'child_success'):
        code = "import pathlib, signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); pathlib.Path('child-ready').touch(); time.sleep(30)"
        child = subprocess.Popen([sys.executable, '-c', code])
        (root / 'child.pid').write_text(str(child.pid))
        while not pathlib.Path('child-ready').exists(): time.sleep(.01)
        print('parent stdout retained', flush=True)
        print('child failure stderr retained', file=sys.stderr, flush=True)
        if mode == 'child_timeout': time.sleep(30)
        if mode != 'child_success': sys.exit(7)
    if mode == 'timeout': time.sleep(10)
    if mode == 'signal':
        print('signal evidence', file=sys.stderr, flush=True)
        os.kill(os.getpid(), signal.SIGTERM)
    if mode == 'failure':
        (root / 'failed.marker').touch()
        print('simulated failure', file=sys.stderr)
        sys.exit(3)
    if mode == 'delete_input':
        input_file.unlink()
        print('deleted input evidence', file=sys.stderr)
        sys.exit(8)
    if mode == 'noisy':
        print('x' * 5000 + 'stdout end')
        print('y' * 5000 + 'stderr end', file=sys.stderr)
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
    assert all("--text" not in call for call in calls)


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


@pytest.mark.parametrize("text", ['「第一」です。『第二』と"第三"です。', "--help を読みます。", "'確認'です。"])
def test_private_file_transport_preserves_text_and_quoted_paths(engine, tmp_path, text):
    parent = tmp_path / '保存先 with spaces and "double" and \'single\''
    output = parent / 'final "audio".wav'
    engine.render(text, output, "Japanese Female 1", pitch=-100)
    observed = json.loads((tmp_path / "observed.json").read_text())
    args = observed["args"]
    assert args[args.index("--text") + 1] == "input.txt"
    assert args[args.index("--out") + 1] == "output.wav"
    assert args[args.index("--pitch") + 1] == "-100"
    assert "--say" not in args and text not in args
    assert bytes.fromhex(observed["text_hex"]) == text.encode("utf-8")
    assert observed["file_mode"] == 0o600
    assert observed["directory_mode"] == 0o700
    assert not os.path.exists(observed["cwd"])
    assert output.is_file()
    calls = [json.loads(line) for line in (tmp_path / "calls.jsonl").read_text().splitlines()]
    assert sum("--text" in call for call in calls) == 1
    assert engine.last_diagnostics["input_transport"] == "utf8_file"
    assert engine.last_diagnostics["input_sha256"] == hashlib.sha256(text.encode()).hexdigest()


@pytest.mark.parametrize("mode", ["child_failure", "child_timeout"])
def test_owned_child_is_killed_after_parent_failure_or_timeout(engine, tmp_path, mode):
    engine.inventory()
    (tmp_path / "mode").write_text(mode)
    engine.timeout = .6 if mode == "child_timeout" else 3
    unrelated = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"], start_new_session=True)
    try:
        with pytest.raises(VPError, match="input was not retried") as failure:
            engine.render("確認です。", tmp_path / "bad.wav", "Japanese Female 1")
        assert failure.value.code == ("environment" if mode == "child_timeout" else "synthesis")
        diagnostics = engine.last_diagnostics
        assert diagnostics["timed_out"] is (mode == "child_timeout")
        assert diagnostics["failure_kind"] == ("timeout" if mode == "child_timeout" else "nonzero_exit")
        assert "child failure stderr retained" in diagnostics["stderr_tail"]
        assert "parent stdout retained" in diagnostics["stdout_tail"]
        assert diagnostics["owned_group_cleanup"]["kill_sent"]
        assert diagnostics["owned_group_cleanup"]["remaining_pids"] == []
        assert voicepeak._group_members(diagnostics["pid"]) == []
        assert unrelated.poll() is None
        assert not list(tmp_path.glob(".vp-render-*"))
    finally:
        unrelated.terminate()
        unrelated.wait(timeout=5)


def test_signal_diagnostics_preserved(engine, tmp_path):
    engine.inventory()
    (tmp_path / "mode").write_text("signal")
    with pytest.raises(VPError, match="SIGTERM"):
        engine.render("確認です。", tmp_path / "bad.wav", "Japanese Female 1")
    assert engine.last_diagnostics["exit_code"] == -signal.SIGTERM
    assert engine.last_diagnostics["signal"] == "SIGTERM"
    assert engine.last_diagnostics["failure_kind"] == "signal"
    assert "signal evidence" in engine.last_diagnostics["stderr_tail"]


@pytest.mark.parametrize("guard", ["foreign_process", "group_check", "dictionary"])
def test_postflight_failure_stops_batch_without_hiding_exit_evidence(engine, tmp_path, monkeypatch, guard):
    engine.inventory()
    (tmp_path / "mode").write_text("failure")
    after_calls = []
    monkeypatch.setattr(voicepeak, "engine_after", lambda: after_calls.append(True))
    if guard == "foreign_process":
        def check():
            if (tmp_path / "failed.marker").exists():
                raise VPError("Close the independently running GUI", "environment")
        monkeypatch.setattr(voicepeak, "ensure_no_voicepeak", check)
    elif guard == "group_check":
        def check_group(_):
            raise VPError("Owned process check unavailable", "environment")
        monkeypatch.setattr(voicepeak, "_group_members", check_group)
    else:
        def after():
            raise VPError("Dictionary ownership uncertain", "needs_recovery")
        monkeypatch.setattr(voicepeak, "engine_after", after)
    with pytest.raises(VPError, match="status 3") as failure:
        engine.render("確認です。", tmp_path / "bad.wav", "Japanese Female 1")
    assert failure.value.code == ("needs_recovery" if guard == "dictionary" else "environment")
    assert engine.last_diagnostics["exit_code"] == 3
    assert engine.last_diagnostics["failure_kind"] == "nonzero_exit"
    assert "simulated failure" in engine.last_diagnostics["stderr_tail"]
    assert engine.last_diagnostics["postflight_error"]
    assert after_calls == []


@pytest.mark.parametrize("mode", ["missing", "broken", "silent"])
def test_invalid_output_retains_successful_exit_and_output_failure(engine, tmp_path, mode):
    engine.inventory()
    (tmp_path / "mode").write_text(mode)
    with pytest.raises(VPError, match="usable WAV"):
        engine.render("確認です。", tmp_path / "bad.wav", "Japanese Female 1")
    assert engine.last_diagnostics["exit_code"] == 0
    assert engine.last_diagnostics["failure_kind"] == "invalid_output"
    assert engine.last_diagnostics["output_error"]


def test_input_removal_does_not_hide_failure(engine, tmp_path):
    engine.inventory()
    (tmp_path / "mode").write_text("delete_input")
    with pytest.raises(VPError, match="status 8"):
        engine.render("確認です。", tmp_path / "bad.wav", "Japanese Female 1")
    assert engine.last_diagnostics["exit_code"] == 8
    assert "deleted input evidence" in engine.last_diagnostics["stderr_tail"]


def test_bounded_diagnostic_tails_reset_between_calls(engine, tmp_path):
    engine.inventory()
    (tmp_path / "mode").write_text("noisy")
    engine.render("確認です。", tmp_path / "good.wav", "Japanese Female 1")
    diagnostics = engine.last_diagnostics
    for name in ("stdout", "stderr"):
        assert diagnostics[f"{name}_truncated"]
        assert len(diagnostics[f"{name}_tail"]) == 4096
        assert diagnostics[f"{name}_tail"].endswith(name + " end\n")
    (tmp_path / "mode").write_text("ok")
    engine.render("確認です。", tmp_path / "good.wav", "Japanese Female 1")
    assert engine.last_diagnostics["stderr_tail"] == ""
    assert not engine.last_diagnostics["stderr_truncated"]


def test_successful_parent_also_cleans_owned_children_before_dictionary_after(engine, tmp_path, monkeypatch):
    engine.inventory()
    (tmp_path / "mode").write_text("child_success")
    observations = []
    def after():
        observations.append(voicepeak._group_members(engine.last_diagnostics["pid"]))
    monkeypatch.setattr(voicepeak, "engine_after", after)
    engine.render("確認です。", tmp_path / "good.wav", "Japanese Female 1")
    assert observations == [[]]
    assert engine.last_diagnostics["owned_group_cleanup"]["kill_sent"]
    assert engine.last_diagnostics["exit_code"] == 0


@pytest.mark.parametrize("mode", ["signal", "failure"])
def test_exit_classification_exists_before_postflight(engine, tmp_path, monkeypatch, mode):
    engine.inventory()
    (tmp_path / "mode").write_text(mode)
    observed = []
    def after():
        observed.append(dict(engine.last_diagnostics))
        raise VPError("Dictionary ownership uncertain", "needs_recovery")
    monkeypatch.setattr(voicepeak, "engine_after", after)
    with pytest.raises(VPError) as failure:
        engine.render("確認です。", tmp_path / "bad.wav", "Japanese Female 1")
    assert failure.value.code == "needs_recovery"
    assert observed[0]["failure_kind"] == ("signal" if mode == "signal" else "nonzero_exit")
    assert observed[0]["stderr_tail"]


def test_launch_error_is_structured_with_fresh_diagnostics(engine, tmp_path, monkeypatch):
    engine.inventory()
    engine.last_diagnostics = {"stderr_tail": "stale evidence"}
    def fail_launch(*args, **kwargs):
        raise OSError("simulated launch unavailable")
    monkeypatch.setattr(voicepeak.subprocess, "Popen", fail_launch)
    with pytest.raises(VPError, match="Cannot start") as failure:
        engine.render("確認です。", tmp_path / "bad.wav", "Japanese Female 1")
    assert failure.value.code == "environment"
    assert engine.last_diagnostics["failure_kind"] == "launch_error"
    assert engine.last_diagnostics["exit_code"] is None
    assert engine.last_diagnostics["stderr_tail"] == ""
    assert not list(tmp_path.glob(".vp-render-*"))
