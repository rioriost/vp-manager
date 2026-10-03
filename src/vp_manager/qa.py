"""Conservative QA: acoustic validity is distinct from pronunciation acceptance."""

from __future__ import annotations

import os
import re
import unicodedata
from difflib import SequenceMatcher
from pathlib import Path

import numpy as np
import soundfile as sf

from .common import VPError, fingerprint, issue, sha256


def normalized(text: str) -> str:
    text = unicodedata.normalize("NFKC", text).casefold()
    return "".join(c for c in text if unicodedata.category(c)[0] in ("L", "N"))


def inspect_audio(path: Path) -> dict:
    try:
        audio, rate = sf.read(path, dtype="float64", always_2d=True)
    except (OSError, RuntimeError) as exc:
        return {"status": "fail", "issues": [issue("unreadable_audio", str(exc), "error")]}
    problems = []
    finite = bool(np.isfinite(audio).all())
    frames = len(audio)
    peak = float(np.max(np.abs(audio))) if finite and frames else None
    rms = float(np.sqrt(np.mean(audio**2))) if finite and frames else None
    if not finite or not frames or rate < 8000 or rate > 192000 or audio.shape[1] not in (1, 2):
        problems.append(issue("invalid_audio", "Invalid samples, dimensions, or sample rate", "error"))
    elif not peak or peak < 1e-5:
        problems.append(issue("silent_audio", "No audible narration detected", "error"))
    if peak is not None and peak >= 0.9999:
        problems.append(issue("clipping", "Samples reach full scale", "error", peak=peak))
    if frames and finite and float(np.max(np.abs(audio[[0, -1]]))) > 0.02:
        problems.append(issue("edge_cut", "Audible energy at file edge needs review"))
    if rms and rms < 0.005:
        problems.append(issue("quiet_audio", "Low level needs listening review", rms=rms))
    return {
        "status": "fail"
        if any(i["severity"] == "error" for i in problems)
        else ("uncertain" if problems else "pass"),
        "issues": problems,
        "sha256": sha256(path),
        "frames": frames,
        "sample_rate": rate,
        "channels": audio.shape[1],
        "duration": frames / rate,
        "peak": peak,
        "rms": rms,
    }


def transcribe(path: Path, model: Path) -> str:
    # Require a downloaded model; a transcription request must not silently upload
    # narration or fetch arbitrary remote model code.
    model = Path(model).expanduser().resolve()
    if not model.is_dir() or not (model / "config.json").is_file():
        raise VPError("ASR requires an existing local Whisper model directory", code="environment")
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"
    try:
        import mlx_whisper
    except ImportError as exc:
        raise VPError("Install vp-manager[asr] for local transcription", code="environment") from exc
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"
    result = mlx_whisper.transcribe(
        str(path), path_or_hf_repo=str(model), language="ja", temperature=0, condition_on_previous_text=False
    )
    return result.get("text", "").strip()


def compare_content(expected: str, transcript: str, important_terms: list[str] | None = None) -> dict:
    left, right = normalized(expected), normalized(transcript)
    similarity = SequenceMatcher(None, left, right).ratio()
    problems = []
    if not right:
        problems.append(issue("empty_asr", "ASR produced no words", "error"))
    elif left != right:
        problems.append(
            issue(
                "asr_difference",
                "ASR differs from the reading copy; inspect readings and omissions",
                similarity=similarity,
                expected=expected,
                transcript=transcript,
            )
        )
    # Number/term mismatch signals review, not a proved TTS error. Japanese ASR
    # commonly converts kana number readings back to digits or kanji.
    numbers = re.findall(r"\d+(?:[.,]\d+)*", unicodedata.normalize("NFKC", expected))
    observed = re.findall(r"\d+(?:[.,]\d+)*", unicodedata.normalize("NFKC", transcript))
    if numbers and numbers != observed:
        problems.append(
            issue(
                "numbers_differ",
                "Numbers require comparison against the original",
                expected=numbers,
                observed=observed,
            )
        )
    meaning_symbols = r"[-+−%％℃°<>≤≥=×÷]"
    expected_symbols = re.findall(meaning_symbols, unicodedata.normalize("NFKC", expected))
    actual_symbols = re.findall(meaning_symbols, unicodedata.normalize("NFKC", transcript))
    if expected_symbols != actual_symbols:
        problems.append(
            issue(
                "symbols_differ",
                "Signs, units or operators need source comparison",
                expected=expected_symbols,
                observed=actual_symbols,
            )
        )
    missing = [t for t in (important_terms or []) if normalized(t) not in right]
    if missing:
        problems.append(issue("terms_unconfirmed", "Important readings not confirmed by ASR", terms=missing))
    return {
        "status": "fail" if not right else ("uncertain" if problems else "pass"),
        "issues": problems,
        "similarity": similarity,
        "transcript": transcript,
    }


def evidence_key(
    audio_hash: str,
    expected: str,
    *,
    content_expected: str | None = None,
    provenance: dict | None = None,
) -> str:
    evidence = {"audio": audio_hash, "expected": expected}
    if content_expected is not None or provenance is not None:
        evidence.update(
            schema_version=2,
            content_expected=expected if content_expected is None else content_expected,
            provenance=provenance,
        )
    return fingerprint(evidence)


def check(
    path: Path,
    expected: str,
    *,
    model: Path | None = None,
    transcript: str | None = None,
    acceptance: dict | None = None,
    content_expected: str | None = None,
    provenance: dict | None = None,
) -> dict:
    acoustics = inspect_audio(path)
    if acoustics["status"] == "fail":
        return {"status": "fail", "acoustics": acoustics, "issues": acoustics["issues"]}
    if transcript is None and model is not None:
        transcript = transcribe(path, model)
    content = (
        compare_content(content_expected or expected, transcript)
        if transcript is not None
        else {"status": "uncertain", "issues": [issue("asr_not_run", "Content has not been checked by ASR")]}
    )
    key = evidence_key(
        acoustics["sha256"], expected, content_expected=content_expected, provenance=provenance
    )
    accepted = bool(
        acceptance
        and acceptance.get("evidence_key") == key
        and acceptance.get("reviewer")
        and acceptance.get("note")
    )
    issues = acoustics["issues"] + content["issues"]
    if not accepted:
        issues.append(
            issue("listening_unreviewed", "Pronunciation and naturalness need listening acceptance")
        )
    # Listening can resolve ASR uncertainty, but never a failed acoustic file check.
    status = (
        "pass"
        if accepted and acoustics["status"] == "pass"
        else ("fail" if content["status"] == "fail" else "uncertain")
    )
    return {
        "status": status,
        "evidence_key": key,
        "acoustics": acoustics,
        "content": {**content, "expected": content_expected or expected},
        "listening": "accepted" if accepted else "unreviewed",
        "issues": issues,
    }
