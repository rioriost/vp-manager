"""Independent regression checks; no live VOICEPEAK process or settings used."""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from vp_manager import lexicon, voicepeak
from vp_manager.common import VPError

ENTRY = {
    "sur": "ReviewSyntheticWord",
    "pron": "サクラモチ",
    "pos": "Japanese_Koyuumeishi_ippan",
    "accentType": 0,
}


@pytest.fixture
def fake_dictionary(tmp_path, monkeypatch):
    settings = tmp_path / "settings"
    settings.mkdir()
    original = {
        "dic.json": '[{"sur":"Existing","pron":"エキシスティング"}]\n'.encode(),
        "user.dic": b"original compiled dictionary\x00\x01",
        "user.csv": b"original,csv\r\n",
    }
    for index, (name, content) in enumerate(original.items()):
        path = settings / name
        path.write_bytes(content)
        path.chmod(0o640)
        os.utime(path, ns=(1_600_000_000_000_000_000, 1_600_000_000_000_000_000 + index))
    monkeypatch.setattr(voicepeak, "LOCK_PATH", tmp_path / "engine.lock")
    monkeypatch.setattr(voicepeak, "ensure_no_voicepeak", lambda: None)
    return settings, tmp_path / "recovery", original


def test_dictionary_restores_observed_derived_changes_and_metadata(fake_dictionary):
    settings, state, original = fake_dictionary
    mtimes = {name: (settings / name).stat().st_mtime_ns for name in original}
    with lexicon.transaction(settings, [ENTRY], state) as result:
        assert result["dictionary_hash"] != lexicon._digest(settings / "user.dic")
        assert len(json.loads((settings / "dic.json").read_text())) == 2
        lexicon.engine_before()
        (settings / "user.dic").write_bytes(b"engine rebuilt binary")
        (settings / "user.csv").write_bytes(b"engine rebuilt csv")
        lexicon.engine_after()
    for name, content in original.items():
        assert (settings / name).read_bytes() == content
        assert stat.S_IMODE((settings / name).stat().st_mode) == 0o640
        assert (settings / name).stat().st_mtime_ns == mtimes[name]
    assert lexicon.recover(settings, state)["status"] == "clean"


def test_external_change_is_retained_and_backup_survives(fake_dictionary):
    settings, state, original = fake_dictionary
    external = b"external dictionary data must survive"
    with pytest.raises(VPError) as error, lexicon.transaction(settings, [ENTRY], state):
        (settings / "user.dic").write_bytes(external)
    assert error.value.code == "needs_recovery"
    assert (settings / "user.dic").read_bytes() == external
    journal = next(state.glob("transaction-*/journal.json"))
    assert (journal.parent / "user.dic").read_bytes() == original["user.dic"]
    assert json.loads(journal.read_text())["status"] == "needs_recovery"
    with pytest.raises(VPError) as retry:
        lexicon.recover(settings, state)
    assert retry.value.code == "needs_recovery"
    assert (settings / "user.dic").read_bytes() == external


def test_process_exit_after_staging_has_durable_recovery(fake_dictionary):
    settings, state, original = fake_dictionary
    script = """
import os, sys
from pathlib import Path
from vp_manager import lexicon, voicepeak
voicepeak.ensure_no_voicepeak = lambda: None
voicepeak.LOCK_PATH = Path(sys.argv[3])
with lexicon.transaction(Path(sys.argv[1]), [{"sur":"ReviewSyntheticWord", "pron":"サクラモチ", "pos":"Japanese_Koyuumeishi_ippan"}], Path(sys.argv[2])):
    os._exit(19)
"""
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
    process = subprocess.run(
        [sys.executable, "-c", script, str(settings), str(state), str(voicepeak.LOCK_PATH)],
        env=environment,
        capture_output=True,
        timeout=20,
        check=False,
    )
    assert process.returncode == 19, process.stderr.decode(errors="replace")
    assert (settings / "dic.json").read_bytes() != original["dic.json"]
    assert lexicon.recover(settings, state)["status"] == "restored"
    assert {name: (settings / name).read_bytes() for name in original} == original


def test_recovery_handles_staged_write_before_journal_update(fake_dictionary, monkeypatch):
    settings, state, original = fake_dictionary
    save = lexicon._save

    def interrupted_save(path, record):
        if record["status"] == "staged":
            raise RuntimeError("simulated process interruption before journal update")
        save(path, record)

    with monkeypatch.context() as context:
        context.setattr(lexicon, "_save", interrupted_save)
        with pytest.raises(RuntimeError), lexicon.transaction(settings, [ENTRY], state):
            pytest.fail("staging must have interrupted before the body")
    assert lexicon.recover(settings, state)["status"] == "restored"
    assert {name: (settings / name).read_bytes() for name in original} == original


def test_recovery_resumes_partially_restored_files(fake_dictionary, monkeypatch):
    settings, state, original = fake_dictionary
    transaction = lexicon.transaction(settings, [ENTRY], state)
    transaction.__enter__()
    lexicon.engine_before()
    (settings / "user.dic").write_bytes(b"engine-owned derivative")
    lexicon.engine_after()
    write = lexicon._write

    def interrupted_write(path, data, mode=0o600):
        write(path, data, mode)
        if path == settings / "dic.json":
            raise RuntimeError("simulated interruption midway through restore")

    with monkeypatch.context() as context:
        context.setattr(lexicon, "_write", interrupted_write)
        with pytest.raises(RuntimeError):
            transaction.__exit__(None, None, None)
    assert (settings / "dic.json").read_bytes() == original["dic.json"]
    assert (settings / "user.dic").read_bytes() == b"engine-owned derivative"
    assert lexicon.recover(settings, state)["status"] == "restored"
    assert {name: (settings / name).read_bytes() for name in original} == original


def test_missing_derived_files_are_removed_after_owned_creation(fake_dictionary):
    settings, state, _ = fake_dictionary
    for name in ("user.dic", "user.csv"):
        (settings / name).unlink()
    with lexicon.transaction(settings, [ENTRY], state):
        lexicon.engine_before()
        (settings / "user.dic").write_bytes(b"new binary")
        (settings / "user.csv").write_bytes(b"new csv")
        lexicon.engine_after()
    assert not (settings / "user.dic").exists()
    assert not (settings / "user.csv").exists()


def test_corrupted_backup_stops_recovery_before_overwriting_live_data(fake_dictionary):
    settings, state, _ = fake_dictionary
    transaction = lexicon.transaction(settings, [ENTRY], state)
    result = transaction.__enter__()
    journal = Path(result["journal"])
    (journal.parent / "user.dic").write_bytes(b"corrupt backup")
    staged = (settings / "dic.json").read_bytes()
    with pytest.raises(VPError) as error:
        transaction.__exit__(None, None, None)
    assert error.value.code == "needs_recovery"
    assert (settings / "dic.json").read_bytes() == staged


@pytest.mark.parametrize("source", ["👍🏽", "☕️", "が", "👩‍💻"])
def test_reading_override_cannot_strand_unicode_sequence(source):
    from vp_manager.decisions import apply_decisions
    from vp_manager.text import ingest_text

    units = ingest_text(source)
    payload = {
        "schema_version": 1,
        "source_revision": "revision",
        "overrides": [
            {"unit_id": units[0]["id"], "start": 0, "end": 1, "expected": source[:1], "reading": "ヨミ"}
        ],
    }
    with pytest.raises(VPError):
        apply_decisions(units, [], "revision", payload)


def test_exact_source_spans_select_one_repeated_term_without_touching_original():
    from vp_manager.decisions import apply_decisions
    from vp_manager.text import detect_candidates, ingest_text

    original = "DBの説明です。次のDBは別の読みです。\r\n"
    units = ingest_text(original)
    target = original.rindex("DB")
    payload = {
        "schema_version": 1,
        "source_revision": "revision",
        "overrides": [
            {
                "unit_id": units[0]["id"],
                "start": target,
                "end": target + 2,
                "expected": "DB",
                "reading": "データベース",
            }
        ],
    }
    result = apply_decisions(units, detect_candidates(units, []), "revision", payload)
    assert units[0]["source_text"] == original
    assert "spoken_text" not in units[0]
    assert result["units"][0]["source_text"] == original
    assert result["units"][0]["spoken_text"] == original[:target] + "データベース" + original[target + 2 :]
    assert len(result["unresolved_candidates"]) == 1


@pytest.mark.parametrize(
    "expected,transcript",
    [
        ("値は-10です", "値は10です"),
        ("値は10%です", "値は10です"),
    ],
)
def test_content_comparison_keeps_numeric_meaning(expected, transcript):
    from vp_manager.qa import compare_content

    result = compare_content(expected, transcript)
    assert result["status"] in {"uncertain", "fail"}
    assert result["issues"]


def test_clean_wave_and_exact_asr_do_not_approve_pronunciation(tmp_path):
    import numpy as np
    import soundfile as sf

    from vp_manager.qa import check

    path = tmp_path / "clean.wav"
    frames = np.arange(48000)
    samples = 0.2 * np.sin(2 * np.pi * 440 * frames / 48000)
    samples[:2400] = samples[-2400:] = 0
    sf.write(path, samples, 48000, subtype="PCM_16")
    without_asr = check(path, "検証です。")
    assert without_asr["status"] == "uncertain"
    exact_asr = check(path, "検証です。", transcript="検証です。")
    assert exact_asr["status"] == "uncertain"
    assert exact_asr["listening"] == "unreviewed"
    approval = {
        "evidence_key": exact_asr["evidence_key"],
        "reviewer": "listener",
        "note": "Listened to this exact file",
    }
    assert check(path, "検証です。", transcript="検証です。", acceptance=approval)["status"] == "pass"
    assert (
        check(path, "変更しました。", transcript="変更しました。", acceptance=approval)["status"]
        == "uncertain"
    )
    samples[5000:6000] *= 0.5
    sf.write(path, samples, 48000, subtype="PCM_16")
    assert check(path, "検証です。", transcript="検証です。", acceptance=approval)["status"] == "uncertain"
