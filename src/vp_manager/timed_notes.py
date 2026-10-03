"""Parse explicit Japanese playback cues while retaining every source character."""

import re
from copy import deepcopy

from .common import VPError

_TIME = re.compile(r"【([0-9]{1,6}):([0-5][0-9])付近】")
_LABELS = {"【再生前】", "【動画を再生】", "【再生後】"}
_CONTROL_LIKE = re.compile(r"【(?:再生|動画|[0-9０-９]+[:：])")


def parse_timed_notes(units: list[dict]) -> dict:
    """Return lossless line units and an optional slide-local playback timeline.

    Plain notes are unchanged. Timed notes use child IDs and parent source offsets
    so reading decisions still address exact immutable spans. Cue times are clip
    offsets; audio durations and whether they fit are checked by the renderer.
    """
    lines = [line for unit in units for line in unit["source_text"].splitlines(keepends=True)]
    mode = any(line.strip() in _LABELS or _CONTROL_LIKE.search(line) for line in lines)
    if not mode:
        return {"units": deepcopy(units), "timeline": None, "issues": []}
    result = []
    timeline = {
        "schema_version": 1,
        "before_unit_ids": [],
        "cues": [],
        "after_unit_ids": [],
        "playback_marker_unit_id": None,
    }
    phase = None
    seen_before = seen_after = False
    current_cue = None
    last_seconds = -1

    def invalid(message):
        raise VPError("Timed notes: " + message, code="needs_decision")

    for source_unit in units:
        offset = 0
        for number, source in enumerate(source_unit["source_text"].splitlines(keepends=True) or [""], 1):
            unit = {
                **deepcopy(source_unit),
                "id": f"{source_unit['id']}_line{number:04d}",
                "source_text": source,
                "source_unit_id": source_unit["id"],
                "source_start": offset,
                "source_end": offset + len(source),
                "spoken_text": source.rstrip("\r\n"),
                "note_control": False,
            }
            offset += len(source)
            label = source.strip()
            timing = _TIME.fullmatch(label)
            if label in _LABELS or timing:
                unit.update(note_control=True, spoken_text="")
                if label == "【再生前】":
                    if seen_before or phase is not None:
                        invalid("【再生前】 must occur once before 【動画を再生】")
                    seen_before, phase = True, "before"
                elif label == "【動画を再生】":
                    if timeline["playback_marker_unit_id"] is not None or seen_after:
                        invalid("exactly one 【動画を再生】 is required")
                    timeline["playback_marker_unit_id"] = unit["id"]
                    phase = "video"
                elif label == "【再生後】":
                    if timeline["playback_marker_unit_id"] is None or seen_after:
                        invalid("【再生後】 must occur once after 【動画を再生】")
                    seen_after, phase = True, "after"
                else:
                    if phase != "video":
                        invalid("time cues belong after 【動画を再生】 and before 【再生後】")
                    seconds = int(timing[1]) * 60 + int(timing[2])
                    if seconds <= last_seconds:
                        invalid("time cues must increase strictly without duplicates")
                    last_seconds = seconds
                    current_cue = {"at_seconds": seconds, "unit_ids": []}
                    timeline["cues"].append(current_cue)
                unit["note_phase"] = "control"
            elif _CONTROL_LIKE.search(label) or (label.startswith("【") and label.endswith("】")):
                invalid(f"unknown or malformed control line {label!r}; use a separate marker line")
            elif label:
                if phase is None:
                    invalid("text before playback requires an explicit 【再生前】 line")
                unit["note_phase"] = phase
                if phase == "video":
                    if current_cue is None:
                        current_cue = {"at_seconds": 0, "unit_ids": []}
                        timeline["cues"].append(current_cue)
                        last_seconds = 0
                    current_cue["unit_ids"].append(unit["id"])
                    unit["cue_seconds"] = current_cue["at_seconds"]
                else:
                    timeline[f"{phase}_unit_ids"].append(unit["id"])
            else:
                unit["note_phase"] = phase
            result.append(unit)
    if timeline["playback_marker_unit_id"] is None:
        invalid("exactly one 【動画を再生】 is required")
    return {"units": result, "timeline": timeline, "issues": []}
