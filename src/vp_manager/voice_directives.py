"""Parse source-bound, line-local VOICEPEAK directions without speaking them."""

import re

from .common import VPError

_PREFIX = re.compile(r"^([ \t]*)【([^】\r\n]+)】")
_NARRATOR = re.compile(r"(?:女性|男性)[1-3]|女の子")
_EMOTIONS = {"楽しい": "fun", "喜び": "happy", "怒り": "angry", "悲しい": "sad"}
_FIELDS = re.compile(r"(速さ|速度|ピッチ)([+-]?\d+)|([一-龯ぁ-んァ-ンA-Za-z]+)(\d+)%")


def parse_prefix(line: str) -> tuple[int, dict | None]:
    """Return the prefix length and explicit fields; unrelated brackets are text."""
    match = _PREFIX.match(line)
    if not match:
        return 0, None
    body = match[2]
    parts = [part.strip() for part in re.split(r"[,、，]", body)]
    if not parts or not (
        _NARRATOR.fullmatch(parts[0])
        or parts[0].startswith(("女性", "男性", "女の子", "速さ", "速度", "ピッチ"))
        or any(parts[0].startswith(name) for name in _EMOTIONS)
    ):
        return 0, None
    if not all(parts):
        raise VPError(f"Empty voice directive field: {body}", code="input")
    options: dict = {}
    for part in parts:
        if _NARRATOR.fullmatch(part):
            if "narrator" in options:
                raise VPError("Narrator is specified twice in one voice directive", code="input")
            options["narrator"] = (
                "Japanese Female Child" if part == "女の子" else
                f"Japanese {'Female' if part.startswith('女性') else 'Male'} {part[-1]}"
            )
            continue
        field = _FIELDS.fullmatch(part)
        if not field:
            raise VPError(f"Invalid voice directive field: {part}", code="input")
        if field[1]:
            key = "speed" if field[1] in {"速さ", "速度"} else "pitch"
            value = int(field[2])
            if key in options or not (50 <= value <= 200 if key == "speed" else -300 <= value <= 300):
                raise VPError(f"Invalid or duplicate {key} in voice directive: {part}", code="input")
            options[key] = value
        else:
            name, value = field[3], int(field[4])
            if name not in _EMOTIONS or not 0 <= value <= 100:
                raise VPError(f"Invalid emotion in voice directive: {part}", code="input")
            emotion = options.setdefault("emotion", {})
            if _EMOTIONS[name] in emotion:
                raise VPError(f"Duplicate emotion in voice directive: {name}", code="input")
            emotion[_EMOTIONS[name]] = value
    return match.end(), options


def source_prefixes(source: str) -> list[tuple[int, dict | None]]:
    return [parse_prefix(line) for line in source.splitlines(keepends=True) or [""]]


def effective_options(defaults: dict, chunk: dict) -> dict:
    """Unspecified fields retain the job defaults; emotions are replaced as a set."""
    return {**defaults, **chunk.get("voice_options", {})}


def render_snapshot(job: dict, chunk: dict) -> dict:
    return effective_options(job["render_snapshot"], chunk)
