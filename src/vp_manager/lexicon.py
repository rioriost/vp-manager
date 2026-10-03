"""Validated additive dictionaries and crash-aware dictionary transactions.

Derived dictionaries are owned only after a bounded engine call has returned.
A crash before that observation deliberately requires manual recovery.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import tempfile
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path

from .common import VPError

FILES = ("dic.json", "user.dic", "user.csv")
POSITIONS = {"Japanese_Koyuumeishi_ippan", "Japanese_Futsuu_meishi"}
_ACTIVE: ContextVar[dict | None] = ContextVar("dictionary_transaction", default=None)


def _digest(path: Path) -> str | None:
    if path.is_symlink():
        raise VPError(f"Dictionary symlinks are not supported: {path}", code="needs_recovery")
    if not path.exists():
        return None
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def _state(settings: Path) -> dict:
    return {name: _digest(settings / name) for name in FILES}


def _sync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _write(path: Path, data: bytes, mode: int = 0o600) -> None:
    fd, temporary = tempfile.mkstemp(prefix=".vp-manager-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            os.fchmod(stream.fileno(), mode)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        _sync_dir(path.parent)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _save(journal: Path, record: dict) -> None:
    _write(journal, (json.dumps(record, ensure_ascii=False, indent=2) + "\n").encode())


def read_dictionary(settings: Path) -> list[dict]:
    path = Path(settings) / "dic.json"
    _digest(path)
    if not path.exists():
        return []
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError) as exc:
        raise VPError(f"Cannot read dictionary: {exc}", code="environment") from exc
    if not isinstance(value, list) or any(not isinstance(e, dict) or not isinstance(e.get("sur"), str) for e in value):
        raise VPError("Dictionary must be a JSON list of entries with sur strings", code="environment")
    return value


def dictionary_hash(settings: Path) -> str:
    # Only dic.json is the authoritative input; derivative recompilation must
    # not change the cache key partway through a batch.
    return _digest(Path(settings) / "dic.json") or hashlib.sha256(b"[]").hexdigest()


def validate_entry(entry: dict) -> dict:
    if not isinstance(entry, dict):
        raise VPError("Dictionary entry must be an object", code="input")
    allowed = {"sur", "pron", "pos", "priority", "accentType", "lang"}
    if set(entry) - allowed:
        raise VPError("Dictionary entry contains unsupported fields", code="input")
    result = {"priority": 5, "accentType": 0, "lang": "ja", **entry}
    surface, reading = result.get("sur"), result.get("pron")
    if not isinstance(surface, str) or not surface.strip() or len(surface) > 140 or any(ord(c) < 32 or ord(c) == 127 for c in surface):
        raise VPError("Dictionary surface must contain 1–140 printable characters", code="input")
    if not isinstance(reading, str) or not reading or len(reading) > 140 or not re.fullmatch(r"[ァ-ヺー]+", reading):
        raise VPError("Dictionary reading must be katakana", code="input")
    if reading[0] in "ァィゥェォャュョヮー":
        raise VPError("Dictionary reading cannot start with a small vowel or long mark", code="input")
    mora = sum(c not in "ァィゥェォャュョヮ" for c in reading)
    if result.get("pos") not in POSITIONS or result["lang"] != "ja":
        raise VPError("Only the two verified Japanese noun positions are supported", code="input")
    if type(result["priority"]) is not int or not 0 <= result["priority"] <= 10:
        raise VPError("Dictionary priority must be an integer from 0 to 10", code="input")
    if type(result["accentType"]) is not int or not 0 <= result["accentType"] <= mora:
        raise VPError("Dictionary accentType must be between zero and the mora count", code="input")
    return result


def _entries(entries: list[dict]) -> list[dict]:
    if not isinstance(entries, list):
        raise VPError("Dictionary entries must be a list", code="input")
    normalized = [validate_entry(entry) for entry in entries]
    if len({entry["sur"] for entry in normalized}) != len(normalized):
        raise VPError("Duplicate dictionary surfaces in requested entries", code="input")
    return normalized


def _canonical(value) -> bytes:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False, separators=(",", ":")).encode()
    except (TypeError, ValueError) as exc:
        raise VPError("Promotion evidence must be finite JSON data", code="input") from exc


def _fingerprint(value) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _dictionary_bytes(entries: list[dict]) -> bytes:
    return (json.dumps(entries, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode()


def _missing(original: list[dict], requested: list[dict]) -> list[dict]:
    missing = []
    for candidate in requested:
        matches = [entry for entry in original if entry["sur"] == candidate["sur"]]
        if not matches:
            missing.append(candidate)
            continue
        try:
            same = len(matches) == 1 and validate_entry(matches[0]) == candidate
        except VPError:
            same = False
        if not same:
            raise VPError(f"Existing dictionary entry conflicts or has unsupported metadata: {candidate['sur']}", code="needs_decision")
    return missing


def preview_dictionary(settings: Path, entries: list[dict]) -> dict:
    """Compare strictly without writes; matching existing entries retain their bytes."""
    normalized = _entries(entries)
    baseline = dictionary_hash(settings)
    original = read_dictionary(settings)
    pending = _missing(original, normalized)
    if dictionary_hash(settings) != baseline:
        raise VPError("Dictionary changed during preview", code="needs_recovery")
    staged = hashlib.sha256(_dictionary_bytes(original + pending)).hexdigest() if pending else baseline
    return {"base_dictionary_hash": baseline, "dictionary_hash": staged, "staged_fingerprint": staged,
            "pending_entries": pending, "entries": normalized}


def pending_additions(settings: Path, entries: list[dict]) -> list[dict]:
    """Return missing entries, not evidence that existing entries were approved."""
    return preview_dictionary(settings, entries)["pending_entries"]


def _validate_receipt(record: dict, journal: Path) -> dict:
    """Validate immutable commit evidence; never compare with today's dictionary."""
    try:
        receipt = record["receipt"]
        request = receipt["request"]
        valid = (
            record["schema_version"] == 2 and record["kind"] == "promotion" and record["status"] == "committed"
            and record["receipt_sha256"] == _fingerprint(receipt)
            and record["operation_id"] == receipt["operation_id"]
            and record["settings"] == request["settings"]
            and record["request_fingerprint"] == _fingerprint(request)
            and record["base_dictionary_hash"] == request["expected_base_hash"]
            and record["staged_fingerprint"] == receipt["committed_dictionary_hash"]
            and record["entries"] == request["entries"] == _entries(request["entries"])
            and isinstance(request["evidence"], dict) and bool(request["evidence"])
            and receipt["pending_regeneration"] is True
            and record["committed_hashes"]["dic.json"] == record["staged_fingerprint"]
            and _digest(journal.parent / "staged.json") == record["staged_fingerprint"]
        )
        for name in FILES:
            valid = valid and _digest(journal.parent / name) == record["before_hashes"][name]
        valid = valid and (record["before_hashes"]["dic.json"] or hashlib.sha256(b"[]").hexdigest()) == request["expected_base_hash"]
        original = read_dictionary(journal.parent)
        valid = valid and not ({entry["sur"] for entry in original} & {entry["sur"] for entry in request["entries"]})
        valid = valid and hashlib.sha256(_dictionary_bytes(original + request["entries"])).hexdigest() == record["staged_fingerprint"]
    except (KeyError, TypeError, ValueError, OSError, VPError) as exc:
        raise VPError(f"Invalid committed dictionary receipt: {journal}", code="needs_recovery") from exc
    if not valid:
        raise VPError(f"Invalid committed dictionary receipt: {journal}", code="needs_recovery")
    return receipt


def _conflict(record: dict, journal: Path, message: str) -> None:
    record["status"] = "needs_recovery"
    record["issue"] = message
    record["observed_hashes"] = _state(Path(record["settings"]))
    _save(journal, record)
    raise VPError(message + f"; backups retained at {journal.parent}", code="needs_recovery")


def engine_before() -> None:
    active = _ACTIVE.get()
    if active is None:
        return
    record, journal = active["record"], active["journal"]
    if _state(Path(record["settings"])) != record["expected_hashes"]:
        _conflict(record, journal, "Dictionary changed outside the transaction")
    record["engine_inflight"] = True
    _save(journal, record)


def engine_after() -> None:
    active = _ACTIVE.get()
    if active is None:
        return
    record, journal = active["record"], active["journal"]
    current = _state(Path(record["settings"]))
    if current["dic.json"] != record["expected_hashes"]["dic.json"]:
        _conflict(record, journal, "Authoritative dictionary changed during synthesis")
    record["expected_hashes"] = current
    record["engine_inflight"] = False
    _save(journal, record)


def _restore(record: dict, journal: Path) -> dict:
    # Validate every restore input before modifying any file; malformed metadata
    # must not produce a partly restored dictionary followed by a traceback.
    try:
        valid = record["status"] != "committed" and isinstance(record["settings"], str)
        valid = valid and bool(re.fullmatch(r"[0-9a-f]{64}", record["staged_fingerprint"]))
        for name in FILES:
            for field in ("before_hashes", "expected_hashes"):
                value = record[field][name]
                valid = valid and (value is None or isinstance(value, str) and bool(re.fullmatch(r"[0-9a-f]{64}", value)))
            metadata = record["metadata"][name]
            if record["before_hashes"][name] is None:
                valid = valid and metadata is None
            else:
                valid = valid and isinstance(metadata, dict) and type(metadata["mode"]) is int and 0 <= metadata["mode"] <= 0o7777
                valid = valid and type(metadata["atime_ns"]) is int and type(metadata["mtime_ns"]) is int
    except (KeyError, TypeError, ValueError) as exc:
        raise VPError(f"Invalid dictionary recovery record: {journal}", code="needs_recovery") from exc
    if not valid:
        raise VPError(f"Invalid dictionary recovery record: {journal}", code="needs_recovery")
    settings = Path(record["settings"])
    current = _state(settings)
    # Restoration itself is restartable. A file may already contain its backup.
    for name in FILES:
        expected = {record["expected_hashes"][name], record["before_hashes"][name]}
        if name == "dic.json":
            expected.add(record["staged_fingerprint"])
        if current[name] not in expected:
            _conflict(record, journal, f"External or unobserved dictionary change: {name}")
        if record["before_hashes"][name] is not None and _digest(journal.parent / name) != record["before_hashes"][name]:
            _conflict(record, journal, f"Backup integrity check failed: {name}")
    record["status"] = "restoring"
    _save(journal, record)
    for name in FILES:
        # Check again immediately before each replacement.
        if _digest(settings / name) != current[name]:
            _conflict(record, journal, f"Dictionary changed during restoration: {name}")
        metadata = record["metadata"][name]
        if record["before_hashes"][name] is None:
            (settings / name).unlink(missing_ok=True)
        else:
            _write(settings / name, (journal.parent / name).read_bytes(), metadata["mode"])
        _sync_dir(settings)
    record["status"] = "restored"
    record["engine_inflight"] = False
    record["observed_hashes"] = _state(settings)
    # Hash verification reads files; restore timestamps after the final read.
    for name in FILES:
        metadata = record["metadata"][name]
        if metadata is not None:
            with (settings / name).open("rb") as stream:
                os.utime(stream.fileno(), ns=(metadata["atime_ns"], metadata["mtime_ns"]))
                os.fsync(stream.fileno())
    _save(journal, record)
    restored_hash = record["observed_hashes"]["dic.json"] or hashlib.sha256(b"[]").hexdigest()
    return {"status": "restored", "journal": str(journal), "dictionary_hash": restored_hash}


def _pending(settings: Path, state_dir: Path):
    for journal in sorted(state_dir.glob("transaction-*/journal.json")):
        try:
            record = json.loads(journal.read_text())
            if record["settings"] == str(settings.resolve()):
                if record["status"] == "committed":
                    _validate_receipt(record, journal)
                elif record["status"] != "restored":
                    if record.get("kind", "temporary") not in {"temporary", "promotion"}:
                        raise VPError(f"Unknown dictionary journal kind: {journal}", code="needs_recovery")
                    yield journal, record
        except (ValueError, KeyError, TypeError, OSError) as exc:
            raise VPError(f"Unreadable recovery journal: {journal}", code="needs_recovery") from exc


def recover(settings: Path, state_dir: Path) -> dict:
    from .voicepeak import engine_session
    settings, state_dir = Path(settings).resolve(), Path(state_dir)
    with engine_session(settings):
        pending = list(_pending(settings, state_dir))
        if len(pending) > 1:
            raise VPError("Multiple pending dictionary journals require manual recovery", code="needs_recovery")
        if not pending:
            return {"status": "clean", "dictionary_hash": dictionary_hash(settings)}
        journal, record = pending[0]
        return _restore(record, journal)


def assert_clean(settings: Path, state_dir: Path) -> None:
    """Read-only gate; never repairs a pending transaction implicitly."""
    if list(_pending(Path(settings).resolve(), Path(state_dir))):
        raise VPError("A dictionary transaction needs recovery first", code="needs_recovery")


def _promotion_result(record: dict, journal: Path, current_hash: str, *, idempotent: bool) -> dict:
    return {"status": "committed", "dictionary_hash": current_hash,
            "committed_dictionary_hash": record["staged_fingerprint"],
            "base_dictionary_hash": record["base_dictionary_hash"],
            "receipt_path": str(journal), "receipt_id": record["operation_id"],
            "pending_regeneration": True, "idempotent": idempotent}


def promote(settings: Path, entries: list[dict], state_dir: Path, *, expected_base_hash: str, evidence: dict) -> dict:
    """Persist new entries after the caller verifies genuine, current listening QA.

    This backend records that evidence but cannot establish who actually listened.
    The durable committed journal is the only commit point. Recovery restores
    every uncommitted installation and never rewrites committed snapshots.
    """
    from .voicepeak import engine_session, ensure_no_voicepeak

    settings, state_dir = Path(settings).resolve(), Path(state_dir).resolve()
    normalized = _entries(entries)
    if not normalized:
        raise VPError("Promotion requires at least one new entry", code="input")
    if not isinstance(expected_base_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", expected_base_hash):
        raise VPError("Promotion requires an exact baseline dictionary SHA-256", code="input")
    if not isinstance(evidence, dict) or not evidence:
        raise VPError("Promotion requires caller-verified listening evidence", code="input")
    # Freeze JSON input so caller mutations cannot change the request mid-write.
    evidence = json.loads(_canonical(evidence))
    request = {"settings": str(settings), "expected_base_hash": expected_base_hash,
               "entries": normalized, "evidence": evidence}
    request_fingerprint = _fingerprint(request)
    with engine_session(settings):
        assert_clean(settings, state_dir)
        original = read_dictionary(settings)
        for journal in sorted(state_dir.glob("transaction-*/journal.json")):
            record = json.loads(journal.read_text())
            if record.get("settings") == str(settings) and record.get("status") == "committed" and record.get("request_fingerprint") == request_fingerprint:
                _validate_receipt(record, journal)
                if _missing(original, normalized):
                    raise VPError("Committed entries were removed; refusing to replay an old snapshot", code="needs_decision")
                return _promotion_result(record, journal, dictionary_hash(settings), idempotent=True)
        if dictionary_hash(settings) != expected_base_hash:
            raise VPError("Dictionary no longer matches the accepted synthesis baseline", code="needs_decision")
        if len(_missing(original, normalized)) != len(normalized):
            raise VPError("Existing entries require a matching committed receipt; no entries were overwritten", code="needs_decision")
        if not settings.is_dir():
            raise VPError("Voicepeak settings directory does not exist", code="environment")
        metadata = {}
        for name in FILES:
            path = settings / name
            if path.exists():
                info = path.stat()
                metadata[name] = {"mode": stat.S_IMODE(info.st_mode), "atime_ns": info.st_atime_ns, "mtime_ns": info.st_mtime_ns}
            else:
                metadata[name] = None
        before = _state(settings)
        if (before["dic.json"] or hashlib.sha256(b"[]").hexdigest()) != expected_base_hash:
            raise VPError("Dictionary changed while preparing promotion", code="needs_recovery")
        operation_id = uuid.uuid4().hex
        state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        _sync_dir(state_dir.parent)
        backup = state_dir / ("transaction-" + operation_id)
        backup.mkdir(mode=0o700)
        _sync_dir(state_dir)
        for name in FILES:
            if before[name] is not None:
                _write(backup / name, (settings / name).read_bytes())
                if _digest(backup / name) != before[name]:
                    raise VPError("Dictionary changed while backing up promotion", code="needs_recovery")
        # Construct the candidate from the proven backup bytes, not an earlier
        # live read that an external writer might have changed in between.
        original = read_dictionary(backup)
        if len(_missing(original, normalized)) != len(normalized):
            raise VPError("Dictionary entries appeared while preparing promotion", code="needs_recovery")
        staged = _dictionary_bytes(original + normalized)
        staged_fingerprint = hashlib.sha256(staged).hexdigest()
        _write(backup / "staged.json", staged)
        record = {"schema_version": 2, "kind": "promotion", "operation_id": operation_id,
                  "settings": str(settings), "status": "prepared", "before_hashes": before,
                  "expected_hashes": before.copy(), "staged_fingerprint": staged_fingerprint,
                  "base_dictionary_hash": expected_base_hash, "metadata": metadata, "engine_inflight": False,
                  "entries": normalized, "request_fingerprint": request_fingerprint}
        receipt = {"schema_version": 1, "operation_id": operation_id, "request": request,
                   "committed_dictionary_hash": staged_fingerprint, "pending_regeneration": True}
        record["receipt"], record["receipt_sha256"] = receipt, _fingerprint(receipt)
        journal = backup / "journal.json"
        _save(journal, record)
        ensure_no_voicepeak()
        if _state(settings) != before:
            _conflict(record, journal, "Dictionary changed before promotion")
        for name in FILES:
            if _digest(backup / name) != before[name]:
                _conflict(record, journal, f"Backup integrity check failed before promotion: {name}")
        if _digest(backup / "staged.json") != staged_fingerprint:
            _conflict(record, journal, "Staged dictionary integrity check failed")
        _write(settings / "dic.json", staged, metadata["dic.json"]["mode"] if metadata["dic.json"] else 0o600)
        record["expected_hashes"]["dic.json"] = staged_fingerprint
        record["status"] = "installed"
        _save(journal, record)
        ensure_no_voicepeak()
        current = _state(settings)
        if current != record["expected_hashes"]:
            _conflict(record, journal, "Dictionary changed before promotion commit")
        record["committed_hashes"] = current
        record["status"] = "committed"
        _save(journal, record)  # Commit point, after dic.json and its directory fsync.
        return _promotion_result(record, journal, staged_fingerprint, idempotent=False)


@contextmanager
def transaction(settings: Path, entries: list[dict], state_dir: Path):
    from .voicepeak import engine_session, ensure_no_voicepeak
    settings, state_dir = Path(settings).resolve(), Path(state_dir).resolve()
    if not isinstance(entries, list):
        raise VPError("Dictionary entries must be a list", code="input")
    normalized = [validate_entry(entry) for entry in entries]
    with engine_session(settings):
        if list(_pending(settings, state_dir)):
            raise VPError("A dictionary transaction needs recovery first", code="needs_recovery")
        metadata = {}
        for name in FILES:
            path = settings / name
            if path.exists():
                info = path.stat()
                metadata[name] = {"mode": stat.S_IMODE(info.st_mode), "atime_ns": info.st_atime_ns, "mtime_ns": info.st_mtime_ns}
            else:
                metadata[name] = None
        original = read_dictionary(settings)
        surfaces = {entry["sur"] for entry in original}
        for entry in normalized:
            if entry["sur"] in surfaces:
                raise VPError(f"Dictionary surface already exists: {entry['sur']}", code="needs_decision")
            surfaces.add(entry["sur"])
        if not normalized:
            yield {"dictionary_hash": dictionary_hash(settings), "staged_fingerprint": dictionary_hash(settings)}
            return
        if not settings.is_dir():
            raise VPError("Voicepeak settings directory does not exist", code="environment")
        state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        _sync_dir(state_dir.parent)
        backup = state_dir / ("transaction-" + uuid.uuid4().hex)
        backup.mkdir(mode=0o700)
        _sync_dir(state_dir)
        before = _state(settings)
        for name in FILES:
            path = settings / name
            if before[name] is not None:
                if metadata[name] is None:
                    raise VPError("Dictionary appeared while preparing a transaction", code="needs_recovery")
                _write(backup / name, path.read_bytes())
                if _digest(backup / name) != before[name]:
                    raise VPError("Dictionary changed while backing up", code="needs_recovery")
        staged = _dictionary_bytes(original + normalized)
        fingerprint = hashlib.sha256(staged).hexdigest()
        record = {"schema_version": 1, "kind": "temporary", "settings": str(settings), "status": "prepared", "before_hashes": before,
                  "expected_hashes": before.copy(), "staged_fingerprint": fingerprint, "metadata": metadata,
                  "engine_inflight": False, "entries": normalized}
        journal = backup / "journal.json"
        _save(journal, record)  # Durable intent before touching any live file.
        ensure_no_voicepeak()
        if _state(settings) != before:
            _conflict(record, journal, "Dictionary changed before staging")
        _write(settings / "dic.json", staged, metadata["dic.json"]["mode"] if metadata["dic.json"] else 0o600)
        record["expected_hashes"]["dic.json"] = fingerprint
        record["status"] = "staged"
        _save(journal, record)
        token = _ACTIVE.set({"record": record, "journal": journal})
        try:
            yield {"dictionary_hash": fingerprint, "staged_fingerprint": fingerprint, "journal": str(journal)}
        finally:
            _ACTIVE.reset(token)
            ensure_no_voicepeak()
            _restore(record, journal)
