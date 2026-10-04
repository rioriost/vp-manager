"""Bounded VOICEPEAK CLI adapter; never controls activation or GUI state."""
from __future__ import annotations

import fcntl
import hashlib
import json
import math
import os
import plistlib
import re
import signal
import subprocess
import tempfile
import time
import unicodedata
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path

import numpy as np
import soundfile as sf

from .common import VPError
from .lexicon import dictionary_hash, engine_after, engine_before

DEFAULT_EXECUTABLE = Path("/Applications/VOICEPEAK.app/Contents/MacOS/voicepeak")
DEFAULT_SETTINGS = Path.home() / "Library/Application Support/Dreamtonics/Voicepeak/settings"
LOCK_PATH = Path.home() / ".local/state/vp-manager/engine.lock"
_SESSION: ContextVar[bool] = ContextVar("engine_session", default=False)
VoicepeakError = VPError


def ensure_no_voicepeak() -> None:
    try:
        result = subprocess.run(["pgrep", "-ix", "voicepeak"], capture_output=True, timeout=5, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise VPError("Unable to check whether VOICEPEAK is already running", code="environment") from exc
    if result.returncode == 0:
        raise VPError("Close the VOICEPEAK GUI and other CLI instances before continuing", code="environment")
    if result.returncode != 1:
        raise VPError("VOICEPEAK process check failed", code="environment")


@contextmanager
def engine_session(settings: Path | None = None):
    if _SESSION.get():
        yield
        return
    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(LOCK_PATH, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise VPError("Another vp-manager engine session is running", code="environment") from exc
        ensure_no_voicepeak()
        token = _SESSION.set(True)
        try:
            yield
        finally:
            _SESSION.reset(token)
    finally:
        os.close(fd)


def _file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def validate_text(text: str) -> None:
    if not isinstance(text, str) or not text.strip() or len(text) > 140:
        raise VPError("Synthesis text must contain 1–140 Unicode code points", code="input")
    if any(unicodedata.category(c).startswith("C") for c in text) or re.search(r"<[^>]*>", text):
        raise VPError("Control characters and markup are not supported synthesis input", code="input")
    from .text import detect_candidates, ingest_text
    if any(candidate["kind"] == "symbol" for candidate in detect_candidates(ingest_text(text), [])):
        raise VPError("Symbol-bearing terms require an explicit safe reading before synthesis", code="needs_decision")


def _group_members(pgid: int) -> list[int]:
    """Observe only our process group; zombies cannot still write dictionary files."""
    try:
        result = subprocess.run(["ps", "-axo", "pid=,pgid=,stat="], capture_output=True,
                                text=True, timeout=5, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise VPError("Unable to check the owned VOICEPEAK process group", "environment") from exc
    if result.returncode:
        raise VPError("Owned VOICEPEAK process-group check failed", "environment")
    members = []
    for line in result.stdout.splitlines():
        fields = line.split()
        if (len(fields) >= 3 and fields[0].isdigit() and fields[1].isdigit()
                and int(fields[1]) == pgid and not fields[2].startswith("Z")):
            members.append(int(fields[0]))
    return members


def _finish_process_group(process: subprocess.Popen) -> dict:
    """Clean up only the session we launched, including after parent exit/crash."""
    cleanup = {"pgid": process.pid, "term_sent": False, "kill_sent": False, "remaining_pids": []}

    def send(sig, key):
        try:
            os.killpg(process.pid, sig)
            cleanup[key] = True
        except ProcessLookupError:
            pass

    # poll() reaps our direct child so it is not mistaken for a live descendant.
    process.poll()
    if _group_members(process.pid):
        send(signal.SIGTERM, "term_sent")
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            process.poll()
            if not _group_members(process.pid):
                break
            time.sleep(.05)
        else:
            send(signal.SIGKILL, "kill_sent")
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            send(signal.SIGKILL, "kill_sent")
            process.wait(timeout=5)
        # A killed grandchild can briefly remain in the process table. Wait for
        # non-zombie members, not for an unrelated init process to reap zombies.
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            cleanup["remaining_pids"] = _group_members(process.pid)
            if not cleanup["remaining_pids"]:
                break
            time.sleep(.05)
    return cleanup


def _stream_diagnostics(stream, limit: int = 4096) -> tuple[str, int]:
    stream.seek(0, os.SEEK_END)
    size = stream.tell()
    stream.seek(max(0, size - limit))
    return stream.read(limit).decode("utf-8", errors="replace"), size


class Voicepeak:
    def __init__(self, executable: Path | None = None, settings: Path | None = None, timeout: float = 60):
        self.executable = Path(executable) if executable else DEFAULT_EXECUTABLE
        self.settings = Path(settings) if settings else DEFAULT_SETTINGS
        if self.executable.resolve() == DEFAULT_EXECUTABLE.resolve() and self.settings.resolve() != DEFAULT_SETTINGS.resolve():
            raise VPError("The installed VOICEPEAK CLI cannot select a custom settings directory", code="environment")
        if not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or not 0 < timeout <= 600:
            raise VPError("Engine timeout must be between zero and 600 seconds", code="input")
        self.timeout = timeout
        self._inventory: dict | None = None
        self._emotions: dict[str, list[str]] = {}
        self.last_diagnostics: dict = {}

    def _run(self, args: list[str], *, cwd: Path | None = None) -> str:
        diagnostics = {
            "exit_code": None, "signal": None, "timed_out": False, "failure_kind": None,
            "stdout_tail": "", "stderr_tail": "", "stdout_truncated": False, "stderr_truncated": False,
            "input_transport": "utf8_file" if "--text" in args else "none", "pid": None,
        }
        self.last_diagnostics = diagnostics
        started = time.monotonic()
        try:
            ensure_no_voicepeak()
            if not self.executable.is_file() or not os.access(self.executable, os.X_OK):
                raise VPError(f"VOICEPEAK executable unavailable: {self.executable}", code="environment")
            engine_before()
        except BaseException:
            diagnostics["failure_kind"] = "preflight"
            raise
        # Disk-backed diagnostics prevent unbounded PIPE memory on noisy engines.
        with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
            process = None
            primary = None
            cleanup_error = None
            postflight_error = None
            try:
                # cwd carries arbitrary parent-path characters outside the CLI
                # parser. Only fixed ASCII input/output basenames enter argv.
                process = subprocess.Popen([str(self.executable.resolve()), *args], cwd=cwd,
                                           stdin=subprocess.DEVNULL, stdout=stdout, stderr=stderr,
                                           start_new_session=True)
                diagnostics["pid"] = process.pid
                process.wait(timeout=self.timeout)
            except BaseException as exc:  # noqa: BLE001 -- cleanup and evidence also survive interruption.
                primary = exc
                diagnostics["timed_out"] = isinstance(exc, subprocess.TimeoutExpired)
            finally:
                if process is not None:
                    try:
                        diagnostics["owned_group_cleanup"] = _finish_process_group(process)
                        if diagnostics["owned_group_cleanup"]["remaining_pids"]:
                            cleanup_error = "Owned VOICEPEAK children remain after termination"
                    except Exception as exc:  # noqa: BLE001 -- retain primary failure even when cleanup fails.
                        cleanup_error = str(exc)
                        # Observation failure must not leave our known group
                        # running. We still fail closed because quiescence was
                        # not verified; never call engine_after in this case.
                        try:
                            os.killpg(process.pid, signal.SIGKILL)
                            diagnostics["cleanup_fallback_kill_sent"] = True
                        except ProcessLookupError:
                            pass
                        except OSError as kill_exc:
                            diagnostics["cleanup_fallback_error"] = str(kill_exc)
                        try:
                            process.wait(timeout=5)
                        except subprocess.TimeoutExpired:
                            pass
                    diagnostics["exit_code"] = process.poll()
                    if process.returncode is not None and process.returncode < 0:
                        try:
                            diagnostics["signal"] = signal.Signals(-process.returncode).name
                        except ValueError:
                            diagnostics["signal"] = f"signal {-process.returncode}"
                # Capture first: postflight checks must never erase crash evidence.
                for name, stream in (("stdout", stdout), ("stderr", stderr)):
                    tail, size = _stream_diagnostics(stream)
                    diagnostics[f"{name}_tail"] = tail
                    diagnostics[f"{name}_bytes"] = size
                    diagnostics[f"{name}_truncated"] = size > 4096
                diagnostics["elapsed_seconds"] = time.monotonic() - started
                if diagnostics["timed_out"]:
                    diagnostics["failure_kind"] = "timeout"
                elif primary is not None:
                    diagnostics["failure_kind"] = "launch_error" if process is None else "interrupted"
                elif diagnostics["exit_code"] != 0:
                    diagnostics["failure_kind"] = "signal" if diagnostics["signal"] else "nonzero_exit"
                if cleanup_error:
                    diagnostics["cleanup_error"] = cleanup_error
                # A foreign GUI is only observed, never terminated. If either
                # check fails, leave the dictionary journal inflight/fail closed.
                try:
                    if cleanup_error:
                        raise VPError(cleanup_error, "environment")
                    ensure_no_voicepeak()
                    engine_after()
                except Exception as exc:  # noqa: BLE001 -- keep original exit diagnostics alongside the guard.
                    postflight_error = exc
                    diagnostics["postflight_error"] = str(exc)
            suffix = f": {diagnostics['stderr_tail']}" if diagnostics["stderr_tail"] else ""
            if postflight_error:
                suffix += f"; postflight safety check: {postflight_error}"
            if diagnostics["timed_out"]:
                diagnostics["failure_kind"] = "timeout"
                raise VPError(f"VOICEPEAK timed out after {self.timeout:g} seconds; input was not retried" + suffix,
                              code="environment") from primary
            if primary is not None:
                diagnostics["failure_kind"] = "launch_error" if process is None else "interrupted"
                if isinstance(primary, OSError):
                    raise VPError(f"Cannot start VOICEPEAK: {primary}" + suffix, code="environment") from primary
                raise primary
            if diagnostics["exit_code"] != 0:
                diagnostics["failure_kind"] = "signal" if diagnostics["signal"] else "nonzero_exit"
                label = f" by {diagnostics['signal']}" if diagnostics["signal"] else ""
                raise VPError(f"VOICEPEAK exited{label} with status {diagnostics['exit_code']}; input was not retried"
                              + suffix, code=(getattr(postflight_error, "code", "environment")
                                             if postflight_error else "synthesis")) from postflight_error
            if postflight_error is not None:
                diagnostics["failure_kind"] = "postflight"
                raise postflight_error
            stdout.seek(0)
            result = stdout.read(1024 * 1024 + 1)
            if len(result) > 1024 * 1024:
                diagnostics["failure_kind"] = "output_limit"
                raise VPError("VOICEPEAK diagnostic output exceeded its bound", code="environment")
            return result.decode("utf-8", errors="replace").strip()

    def inventory(self) -> dict:
        with engine_session(self.settings):
            plist = self.executable.parent.parent / "Info.plist"
            if plist.exists():
                try:
                    version = str(plistlib.loads(plist.read_bytes())["CFBundleShortVersionString"])
                except (ValueError, KeyError, plistlib.InvalidFileException) as exc:
                    raise VPError("Cannot read VOICEPEAK bundle version", code="environment") from exc
            else:
                version = self._run(["--version"])
            narrators = self._run(["--list-narrator"]).splitlines()
            narrators = [value.strip() for value in narrators if value.strip()]
            if not version or not narrators:
                raise VPError("VOICEPEAK returned an empty version or narrator inventory", code="environment")
            roots = [self.settings.parent / "storage", self.executable.parent.parent / "Resources"]
            assets = sorted({p.resolve() for root in roots if root.exists() for p in root.rglob("*") if p.suffix in {".sylapack", ".ppkg"} and p.is_file()})
            if not assets:
                raise VPError("No VOICEPEAK voice assets found; cache identity cannot be established", code="environment")
            asset_hashes = [{"name": str(p), "sha256": _file_hash(p)} for p in assets]
            fingerprint = hashlib.sha256(json.dumps({"version": version, "assets": asset_hashes}, sort_keys=True).encode()).hexdigest()
            self._inventory = {"version": version, "narrators": narrators,
                               "voice_asset_fingerprint": fingerprint, "voice_asset_count": len(assets),
                               "dictionary_hash": dictionary_hash(self.settings)}
            return dict(self._inventory)

    def validate_input(self, text: str, narrator: str, speed=100, pitch=0, emotion: dict | None = None) -> None:
        validate_text(text)
        if type(speed) is not int or not 50 <= speed <= 200 or type(pitch) is not int or not -300 <= pitch <= 300:
            raise VPError("Speed must be 50–200 and pitch -300–300 (integers)", code="input")
        if emotion is not None and not isinstance(emotion, dict):
            raise VPError("Emotion must be an object", code="input")
        with engine_session(self.settings):
            inventory = self._inventory or self.inventory()
            if narrator not in inventory["narrators"]:
                raise VPError("Narrator is not in the verified VOICEPEAK inventory", code="input")
            if emotion:
                if narrator not in self._emotions:
                    self._emotions[narrator] = self._run(["--list-emotion", narrator]).splitlines()
                available = self._emotions[narrator]
                if any(k not in available or type(v) is not int or not 0 <= v <= 100 for k, v in emotion.items()):
                    raise VPError("Emotion names or values are invalid", code="input")

    def render(self, text: str, output: Path, narrator: str, speed=100, pitch=0, emotion: dict | None = None) -> dict:
        with engine_session(self.settings):
            self.validate_input(text, narrator, speed, pitch, emotion)
            inventory = self._inventory
            args = ["--narrator", narrator, "--speed", str(speed), "--pitch", str(pitch)]
            if emotion:
                args += ["--emotion", ",".join(f"{key}={value}" for key, value in sorted(emotion.items()))]
            output = Path(output)
            output.parent.mkdir(parents=True, exist_ok=True)
            started = time.monotonic()
            before = dictionary_hash(self.settings)
            with tempfile.TemporaryDirectory(prefix=".vp-render-", dir=output.parent) as temporary:
                wav = Path(temporary) / "output.wav"
                input_file = Path(temporary) / "input.txt"
                descriptor = os.open(input_file, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                with os.fdopen(descriptor, "wb") as stream:
                    stream.write(text.encode("utf-8"))
                try:
                    self._run(args + ["--text", "input.txt", "--out", "output.wav"], cwd=Path(temporary))
                finally:
                    self.last_diagnostics.update(input_transport="utf8_file", input_sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
                                                 input_codepoints=len(text))
                if dictionary_hash(self.settings) != before:
                    self.last_diagnostics["failure_kind"] = "dictionary_changed"
                    raise VPError("Dictionary changed during synthesis", code="needs_recovery")
                try:
                    info = sf.info(wav)
                    if info.format != "WAV" or info.frames <= 0 or info.samplerate <= 0 or info.channels not in (1, 2) or info.duration > 600:
                        raise ValueError("unexpected audio format or duration")
                    samples, sample_rate = sf.read(wav, always_2d=True, dtype="float32")
                    if not np.isfinite(samples).all() or np.max(np.abs(samples)) <= 1e-7:
                        raise ValueError("non-finite or silent audio")
                except (OSError, RuntimeError, ValueError) as exc:
                    self.last_diagnostics.update(failure_kind="invalid_output", output_error=str(exc))
                    raise VPError(f"VOICEPEAK did not produce usable WAV audio: {exc}", code="synthesis") from exc
                peak = float(np.max(np.abs(samples)))
                metrics = {"sample_rate": sample_rate, "channels": samples.shape[1], "frames": len(samples),
                           "duration": len(samples) / sample_rate, "peak": peak,
                           "rms": float(np.sqrt(np.mean(samples.astype(np.float64) ** 2))),
                           "clipped_fraction": float(np.mean(np.abs(samples) >= 0.999)),
                           "sha256": _file_hash(wav)}
                wav.chmod(0o600)
                with wav.open("rb") as stream:
                    os.fsync(stream.fileno())
                os.replace(wav, output)
                directory_fd = os.open(output.parent, os.O_RDONLY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
            return {**metrics, "path": str(output), "elapsed_seconds": time.monotonic() - started,
                    "version": inventory["version"], "voice_asset_fingerprint": inventory["voice_asset_fingerprint"],
                    "dictionary_hash": before, "narrator": narrator, "speed": speed, "pitch": pitch, "emotion": emotion or {}}
