#!/usr/bin/env python3
"""Stop hook: send the reply back once if its prose uses a banned phrase.

Banned phrases live in banned_phrases.txt next to this script, one per line. Code blocks
and inline code are ignored. Only the first stop is checked (executionNum 1), so the hook
can never loop. Fails open on any error.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

PHRASES_FILE = Path(__file__).with_name("banned_phrases.txt")
FIRST_EXECUTION = 1
UNICODE_ESCAPE = re.compile(r"\\u([0-9a-fA-F]{4})")


def banned_phrases() -> list[str]:
    try:
        lines = PHRASES_FILE.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    phrases = [line.strip() for line in lines if line.strip() and not line.startswith("#")]
    return [UNICODE_ESCAPE.sub(lambda match: chr(int(match.group(1), 16)), phrase) for phrase in phrases]


def last_reply(transcript_path: str) -> str:
    reply = ""
    with open(transcript_path, encoding="utf-8", errors="replace") as transcript:
        for line in transcript:
            if '"PLANNER_RESPONSE"' not in line:
                continue
            try:
                step = json.loads(line)
            except ValueError:
                continue
            if step.get("type") == "PLANNER_RESPONSE" and step.get("content"):
                reply = step["content"]
    return reply


def decide(payload: dict) -> dict:
    if (payload.get("executionNum") or FIRST_EXECUTION) > FIRST_EXECUTION:
        return {"decision": "allow"}
    transcript_path = payload.get("transcriptPath")
    if not transcript_path or not Path(transcript_path).is_file():
        return {"decision": "allow"}
    prose = re.sub(r"```.*?```", "", last_reply(transcript_path), flags=re.DOTALL)
    prose = re.sub(r"`[^`]+`", "", prose)
    for phrase in banned_phrases():
        if re.search(rf"(?<!\w){re.escape(phrase)}(?!\w)", prose, re.IGNORECASE):
            return {"decision": "continue", "reason": f"Rewrite the reply without '{phrase}'."}
    return {"decision": "allow"}


def main() -> None:
    try:
        result = decide(json.loads(sys.stdin.read() or "{}"))
    except Exception:
        result = {"decision": "allow"}
    print(json.dumps(result))


if __name__ == "__main__":
    main()
