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

    def _run(self, args: list[str]) -> str:
        ensure_no_voicepeak()
        if not self.executable.is_file() or not os.access(self.executable, os.X_OK):
            raise VPError(f"VOICEPEAK executable unavailable: {self.executable}", code="environment")
        engine_before()
        # Disk-backed diagnostics prevent unbounded PIPE memory on noisy engines.
        with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
            try:
                process = subprocess.Popen([str(self.executable), *args], stdin=subprocess.DEVNULL,
                                           stdout=stdout, stderr=stderr, start_new_session=True)
            except OSError as exc:
                engine_after()
                raise VPError(f"Cannot start VOICEPEAK: {exc}", code="environment") from exc
            interrupted = None
            try:
                process.wait(timeout=self.timeout)
            except BaseException as exc:  # noqa: BLE001 -- always terminate children, then re-raise interruption.
                interrupted = exc
                try:
                    os.killpg(process.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    process.wait(timeout=5)
                # The parent may terminate before an ignoring child. Kill the
                # complete group even after wait() reports parent completion.
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            ensure_no_voicepeak()
            engine_after()
            stderr.seek(0, os.SEEK_END)
            length = stderr.tell()
            stderr.seek(max(0, length - 4096))
            diagnostic = stderr.read(4096).decode("utf-8", errors="replace")
            self.last_diagnostics = {"exit_code": process.returncode, "stderr_tail": diagnostic,
                                     "stderr_truncated": length > 4096}
            if isinstance(interrupted, subprocess.TimeoutExpired):
                raise VPError("VOICEPEAK timed out; check activation/setup before retrying" + (f": {diagnostic}" if diagnostic else ""), code="environment")
            if interrupted is not None:
                raise interrupted
            if process.returncode != 0:
                raise VPError(f"VOICEPEAK exited with status {process.returncode}; input was not retried" + (f": {diagnostic}" if diagnostic else ""), code="synthesis")
            stdout.seek(0)
            result = stdout.read(1024 * 1024 + 1)
            if len(result) > 1024 * 1024:
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
                self._run(args + ["--say", text, "--out", str(wav)])
                if dictionary_hash(self.settings) != before:
                    raise VPError("Dictionary changed during synthesis", code="needs_recovery")
                try:
                    info = sf.info(wav)
                    if info.format != "WAV" or info.frames <= 0 or info.samplerate <= 0 or info.channels not in (1, 2) or info.duration > 600:
                        raise ValueError("unexpected audio format or duration")
                    samples, sample_rate = sf.read(wav, always_2d=True, dtype="float32")
                    if not np.isfinite(samples).all() or np.max(np.abs(samples)) <= 1e-7:
                        raise ValueError("non-finite or silent audio")
                except (OSError, RuntimeError, ValueError) as exc:
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
