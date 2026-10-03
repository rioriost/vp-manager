import json
import subprocess
import sys
from pathlib import Path

import pytest

from vp_manager import lexicon, voicepeak
from vp_manager.common import VPError

ENTRY = {"sur": "架空語", "pron": "サクラモチ", "pos": "Japanese_Koyuumeishi_ippan"}


@pytest.fixture
def settings(tmp_path, monkeypatch):
    monkeypatch.setattr(voicepeak, "ensure_no_voicepeak", lambda: None)
    monkeypatch.setattr(voicepeak, "LOCK_PATH", tmp_path / "engine.lock")
    path = tmp_path / "settings"
    path.mkdir()
    (path / "dic.json").write_text("[]\n")
    (path / "dic.json").chmod(0o640)
    (path / "user.dic").write_bytes(b"old binary")
    return path


def test_stages_new_entry_and_restores_all_files_with_metadata(settings, tmp_path):
    original = {name: (settings / name).read_bytes() if (settings / name).exists() else None for name in lexicon.FILES}
    info = (settings / "dic.json").stat()
    with lexicon.transaction(settings, [ENTRY], tmp_path / "state") as staged:
        assert lexicon.read_dictionary(settings)[0]["pron"] == "サクラモチ"
        assert lexicon.dictionary_hash(settings) == staged["staged_fingerprint"]
        journal = json.loads(Path(staged["journal"]).read_text())
        assert journal["status"] == "staged"
        lexicon.engine_before()
        (settings / "user.dic").write_bytes(b"engine compiled")
        (settings / "user.csv").write_bytes(b"engine csv")
        lexicon.engine_after()
    for name, value in original.items():
        assert ((settings / name).read_bytes() if (settings / name).exists() else None) == value
    assert (settings / "dic.json").stat().st_mode == info.st_mode
    assert (settings / "dic.json").stat().st_mtime_ns == info.st_mtime_ns
    assert lexicon.recover(settings, tmp_path / "state")["status"] == "clean"


def test_body_exception_still_restores(settings, tmp_path):
    before = (settings / "dic.json").read_bytes()
    with pytest.raises(RuntimeError, match="body"), lexicon.transaction(settings, [ENTRY], tmp_path / "state"):
        raise RuntimeError("body")
    assert (settings / "dic.json").read_bytes() == before


def test_external_change_blocks_restore_and_recovery(settings, tmp_path):
    with pytest.raises(VPError, match="External"), lexicon.transaction(settings, [ENTRY], tmp_path / "state"):
        (settings / "user.dic").write_bytes(b"external work")
    assert (settings / "user.dic").read_bytes() == b"external work"
    assert lexicon.read_dictionary(settings)[0]["sur"] == ENTRY["sur"]
    with pytest.raises(VPError):
        lexicon.recover(settings, tmp_path / "state")
    assert next((tmp_path / "state").glob("transaction-*/user.dic")).read_bytes() == b"old binary"
    with pytest.raises(VPError):
        lexicon.assert_clean(settings, tmp_path / "state")


def test_existing_entry_and_duplicate_candidate_are_never_overwritten(settings, tmp_path):
    (settings / "dic.json").write_text(json.dumps([lexicon.validate_entry(ENTRY)]))
    before = (settings / "dic.json").read_bytes()
    with pytest.raises(VPError, match="already exists"), lexicon.transaction(settings, [ENTRY], tmp_path / "state"):
        pytest.fail("must not stage duplicates")
    assert (settings / "dic.json").read_bytes() == before


@pytest.mark.parametrize("changes", [{"pron": "ひらがな"}, {"pos": "unknown"}, {"accentType": 6}, {"priority": True}, {"lang": "en"}, {"extra": 1}, {"sur": ""}, {"pron": "ャク"}])
def test_entry_validation_rejects_unsafe_values(changes):
    with pytest.raises(VPError):
        lexicon.validate_entry({**ENTRY, **changes})


def test_mora_count_handles_small_kana():
    entry = {**ENTRY, "pron": "キャット", "accentType": 3}
    assert lexicon.validate_entry(entry)["accentType"] == 3
    with pytest.raises(VPError):
        lexicon.validate_entry({**entry, "accentType": 4})


def test_symlink_dictionary_fails_closed(settings, tmp_path):
    target = tmp_path / "outside"
    target.write_text("[]")
    (settings / "dic.json").unlink()
    (settings / "dic.json").symlink_to(target)
    with pytest.raises(VPError), lexicon.transaction(settings, [ENTRY], tmp_path / "state"):
        pytest.fail("symlink must not be used")
    assert target.read_text() == "[]"


EVIDENCE = {"source_revision": "synthetic-source", "reviewer": "synthetic-test-fixture", "audio_hash": "synthetic-audio"}


def test_promote_commits_only_authoritative_dictionary(settings, tmp_path):
    state = tmp_path / "state"
    original = [{"sur": "既存", "pron": "キソン", "unrecognized_metadata": {"keep": True}}]
    (settings / "dic.json").write_text(json.dumps(original, ensure_ascii=False))
    before = lexicon.dictionary_hash(settings)
    preview = lexicon.preview_dictionary(settings, [ENTRY])
    result = lexicon.promote(settings, [ENTRY], state, expected_base_hash=before, evidence=EVIDENCE)
    assert result["status"] == "committed"
    assert result["idempotent"] is False and result["pending_regeneration"] is True
    assert result["dictionary_hash"] == preview["staged_fingerprint"]
    assert lexicon.read_dictionary(settings) == original + [lexicon.validate_entry(ENTRY)]
    assert (settings / "user.dic").read_bytes() == b"old binary"
    assert not (settings / "user.csv").exists()
    assert Path(result["receipt_path"]).stat().st_mode & 0o777 == 0o600
    assert lexicon.recover(settings, state)["status"] == "clean"
    lexicon.assert_clean(settings, state)


def test_preview_skips_only_exact_semantic_matches_without_writes(settings):
    # Missing default fields still mean the same entry; original bytes survive.
    (settings / "dic.json").write_text(json.dumps([ENTRY], ensure_ascii=False))
    before = (settings / "dic.json").read_bytes()
    preview = lexicon.preview_dictionary(settings, [lexicon.validate_entry(ENTRY)])
    assert preview["pending_entries"] == []
    assert preview["dictionary_hash"] == lexicon.dictionary_hash(settings)
    assert (settings / "dic.json").read_bytes() == before
    (settings / "dic.json").write_text(json.dumps([{**ENTRY, "extra": "must not discard"}]))
    with pytest.raises(VPError, match="unsupported metadata"):
        lexicon.pending_additions(settings, [ENTRY])


def test_promotion_replay_keeps_later_dictionary_and_derivative_edits(settings, tmp_path):
    state = tmp_path / "state"
    baseline = lexicon.dictionary_hash(settings)
    first = lexicon.promote(settings, [ENTRY], state, expected_base_hash=baseline, evidence=EVIDENCE)
    later = lexicon.read_dictionary(settings) + [{"sur": "後の語", "pron": "アトノゴ", "custom": 7}]
    (settings / "dic.json").write_text(json.dumps(later, ensure_ascii=False) + "\n\n")
    (settings / "user.dic").write_bytes(b"later engine regeneration")
    current_bytes = (settings / "dic.json").read_bytes()
    replay = lexicon.promote(settings, [ENTRY], state, expected_base_hash=baseline, evidence=EVIDENCE)
    assert replay["idempotent"] is True
    assert replay["receipt_id"] == first["receipt_id"]
    assert replay["committed_dictionary_hash"] == first["dictionary_hash"]
    assert replay["dictionary_hash"] == lexicon.dictionary_hash(settings)
    assert (settings / "dic.json").read_bytes() == current_bytes
    assert (settings / "user.dic").read_bytes() == b"later engine regeneration"
    assert lexicon.recover(settings, state)["status"] == "clean"


@pytest.mark.parametrize("mutation", ["remove", "change", "evidence", "receipt"])
def test_promotion_replay_rejects_mismatched_current_state_or_receipt(settings, tmp_path, mutation):
    state = tmp_path / "state"
    baseline = lexicon.dictionary_hash(settings)
    result = lexicon.promote(settings, [ENTRY], state, expected_base_hash=baseline, evidence=EVIDENCE)
    evidence = EVIDENCE
    if mutation == "remove":
        (settings / "dic.json").write_text("[]")
    elif mutation == "change":
        (settings / "dic.json").write_text(json.dumps([{**lexicon.validate_entry(ENTRY), "pron": "タケノコ"}]))
    elif mutation == "evidence":
        evidence = {**EVIDENCE, "reviewer": "changed"}
    else:
        journal = Path(result["receipt_path"])
        record = json.loads(journal.read_text())
        record["operation_id"] = "corrupt-receipt"
        journal.write_text(json.dumps(record))
    before = (settings / "dic.json").read_bytes()
    with pytest.raises(VPError):
        lexicon.promote(settings, [ENTRY], state, expected_base_hash=baseline, evidence=evidence)
    assert (settings / "dic.json").read_bytes() == before


def test_promotion_rejects_wrong_baseline_or_unreceipted_existing_entry(settings, tmp_path):
    baseline = lexicon.dictionary_hash(settings)
    with pytest.raises(VPError, match="baseline"):
        lexicon.promote(settings, [ENTRY], tmp_path / "state", expected_base_hash="0" * 64, evidence=EVIDENCE)
    assert lexicon.dictionary_hash(settings) == baseline
    assert not (tmp_path / "state").exists()
    (settings / "dic.json").write_text(json.dumps([ENTRY]))
    with pytest.raises(VPError, match="committed receipt"):
        lexicon.promote(settings, [ENTRY], tmp_path / "state", expected_base_hash=lexicon.dictionary_hash(settings), evidence=EVIDENCE)


@pytest.mark.parametrize("boundary", ["backup", "intent", "before_install", "after_install", "installed", "before_commit", "after_commit"])
def test_promotion_hard_exit_boundaries_are_recoverable(settings, tmp_path, boundary):
    state = tmp_path / "state"
    original = (settings / "dic.json").read_bytes()
    # The subprocess bypasses no protection outside this isolated fake settings
    # fixture, and never starts an actual VOICEPEAK executable.
    script = '''
import json, os, sys
from pathlib import Path
from vp_manager import lexicon, voicepeak
settings, state = Path(sys.argv[1]), Path(sys.argv[2])
boundary = sys.argv[3]
voicepeak.LOCK_PATH = state.parent / 'child-engine.lock'
voicepeak.ensure_no_voicepeak = lambda: None
save, write = lexicon._save, lexicon._write
def injected_save(path, record):
    if boundary == 'before_commit' and record['status'] == 'committed': os._exit(71)
    save(path, record)
    if boundary == 'intent' and record['status'] == 'prepared': os._exit(71)
    if boundary == 'installed' and record['status'] == 'installed': os._exit(71)
    if boundary == 'after_commit' and record['status'] == 'committed': os._exit(71)
def injected_write(path, data, mode=0o600):
    if boundary == 'before_install' and path == settings / 'dic.json': os._exit(71)
    write(path, data, mode)
    if boundary == 'backup' and path.name == 'user.dic': os._exit(71)
    if boundary == 'after_install' and path == settings / 'dic.json': os._exit(71)
lexicon._save, lexicon._write = injected_save, injected_write
lexicon.promote(settings, json.loads(sys.argv[4]), state, expected_base_hash=lexicon.dictionary_hash(settings), evidence={'synthetic': True})
'''
    process = subprocess.run([sys.executable, "-c", script, str(settings), str(state), boundary, json.dumps([ENTRY])], timeout=10, check=False)
    assert process.returncode == 71
    result = lexicon.recover(settings, state)
    if boundary == "after_commit":
        assert result["status"] == "clean"
        assert lexicon.read_dictionary(settings) == [lexicon.validate_entry(ENTRY)]
        # Recovery of the same committed transaction is always nonmutating.
        committed = (settings / "dic.json").read_bytes()
        lexicon.recover(settings, state)
        assert (settings / "dic.json").read_bytes() == committed
    else:
        assert result["status"] in {"clean", "restored"}
        assert (settings / "dic.json").read_bytes() == original
    assert (settings / "user.dic").read_bytes() == b"old binary"


@pytest.mark.parametrize("changed", ["external", "backup"])
def test_promotion_conflicts_preserve_live_files_and_backups(settings, tmp_path, monkeypatch, changed):
    state = tmp_path / "state"
    baseline = lexicon.dictionary_hash(settings)
    save = lexicon._save

    def injected(path, record):
        save(path, record)
        if record["status"] == "prepared":
            if changed == "external":
                (settings / "dic.json").write_text('[{"sur":"外部編集"}]')
            else:
                (path.parent / "user.dic").write_bytes(b"damaged backup")

    monkeypatch.setattr(lexicon, "_save", injected)
    with pytest.raises(VPError):
        lexicon.promote(settings, [ENTRY], state, expected_base_hash=baseline, evidence=EVIDENCE)
    preserved = (settings / "dic.json").read_bytes()
    with pytest.raises(VPError):
        lexicon.recover(settings, state)
    assert (settings / "dic.json").read_bytes() == preserved
    assert (settings / "user.dic").read_bytes() == b"old binary"
    assert list(state.glob("transaction-*/journal.json"))


def test_scalar_journal_is_structured_recovery_error(settings, tmp_path):
    state = tmp_path / "state"
    folder = state / "transaction-corrupt"
    folder.mkdir(parents=True)
    (folder / "journal.json").write_text("[]")
    with pytest.raises(VPError) as failure:
        lexicon.recover(settings, state)
    assert failure.value.code == "needs_recovery"


def test_corrupt_restore_metadata_cannot_partly_restore_promotion(settings, tmp_path, monkeypatch):
    state = tmp_path / "state"
    save = lexicon._save

    def stop_installed(path, record):
        save(path, record)
        if record["status"] == "installed":
            raise RuntimeError("interrupted before commit")

    monkeypatch.setattr(lexicon, "_save", stop_installed)
    with pytest.raises(RuntimeError):
        lexicon.promote(settings, [ENTRY], state, expected_base_hash=lexicon.dictionary_hash(settings), evidence=EVIDENCE)
    monkeypatch.setattr(lexicon, "_save", save)
    staged = (settings / "dic.json").read_bytes()
    journal = next(state.glob("transaction-*/journal.json"))
    record = json.loads(journal.read_text())
    record["metadata"]["user.dic"] = None
    journal.write_text(json.dumps(record))
    with pytest.raises(VPError, match="Invalid dictionary recovery record"):
        lexicon.recover(settings, state)
    assert (settings / "dic.json").read_bytes() == staged
    assert (settings / "user.dic").read_bytes() == b"old binary"


def test_gui_opening_before_commit_leaves_recoverable_intent(settings, tmp_path, monkeypatch):
    original = (settings / "dic.json").read_bytes()
    calls = 0

    def check_gui():
        nonlocal calls
        calls += 1
        if calls == 3:
            raise VPError("GUI opened", code="environment")

    monkeypatch.setattr(voicepeak, "ensure_no_voicepeak", check_gui)
    with pytest.raises(VPError, match="GUI opened"):
        lexicon.promote(settings, [ENTRY], tmp_path / "state", expected_base_hash=lexicon.dictionary_hash(settings), evidence=EVIDENCE)
    assert lexicon.recover(settings, tmp_path / "state")["status"] == "restored"
    assert (settings / "dic.json").read_bytes() == original
