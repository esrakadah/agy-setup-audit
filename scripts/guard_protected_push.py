#!/usr/bin/env python3
"""PreToolUse guard: deny git pushes to protected branches, ask before force pushes.

Reads the Antigravity PreToolUse payload on stdin and prints a decision on stdout.
Protected branches come from AGY_PROTECTED_BRANCHES (comma separated), default main,master.
Fails open: if the payload cannot be read, the tool call is allowed, so a broken guard
never locks the agent. Test it with the cases in tests/test_guard.py.
"""
from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import sys

DEFAULT_PROTECTED_BRANCHES = "main,master"
GIT_TIMEOUT_SECONDS = 3
SEGMENT_SEPARATOR = re.compile(r"&&|\|\||;|\||\n")
FORCE_FLAGS = {"-f", "--force", "--force-with-lease", "--force-if-includes"}
PUSH_EVERYTHING_FLAGS = {"--all", "--mirror"}
GIT_OPTIONS_WITH_VALUE = {"-C", "-c", "--git-dir", "--work-tree", "--namespace"}


def protected_branches() -> set[str]:
    configured = os.environ.get("AGY_PROTECTED_BRANCHES", DEFAULT_PROTECTED_BRANCHES)
    return {branch.strip() for branch in configured.split(",") if branch.strip()}


def push_arguments(segment: str) -> tuple[list[str], str | None] | None:
    try:
        tokens = shlex.split(segment)
    except ValueError:
        return None
    if not tokens or os.path.basename(tokens[0]) != "git":
        return None
    git_dir = None
    index = 1
    while index < len(tokens) and tokens[index].startswith("-"):
        if tokens[index] in GIT_OPTIONS_WITH_VALUE and index + 1 < len(tokens):
            if tokens[index] == "-C":
                git_dir = tokens[index + 1]
            index += 2
        else:
            index += 1
    if index >= len(tokens) or tokens[index] != "push":
        return None
    return tokens[index + 1 :], git_dir


def current_branch(directory: str | None) -> str | None:
    if not directory:
        return None
    try:
        result = subprocess.run(
            ["git", "-C", directory, "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True,
            text=True,
            timeout=GIT_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout.strip() or None if result.returncode == 0 else None


def destination_branch(refspec: str, working_branch: str | None) -> str:
    destination = refspec.lstrip("+").split(":", 1)[-1]
    if destination == "HEAD" and working_branch:
        destination = working_branch
    return destination.removeprefix("refs/heads/")


def decide(payload: dict) -> dict:
    tool_call = payload.get("toolCall") or {}
    if tool_call.get("name") != "run_command":
        return {"decision": "allow"}
    arguments = tool_call.get("args") or {}
    command_line = arguments.get("CommandLine") or ""
    workspace_paths = payload.get("workspacePaths") or [None]
    protected = protected_branches()

    for segment in SEGMENT_SEPARATOR.split(command_line):
        parsed = push_arguments(segment.strip())
        if parsed is None:
            continue
        push_tokens, git_dir = parsed
        working_branch = current_branch(git_dir or arguments.get("Cwd") or workspace_paths[0])
        flags = {token for token in push_tokens if token.startswith("-")}
        positional = [token for token in push_tokens if not token.startswith("-")]
        refspecs = positional[1:]

        if flags & PUSH_EVERYTHING_FLAGS:
            return deny(f"'{segment.strip()}' pushes every branch, including protected ones.")
        targets = {destination_branch(refspec, working_branch) for refspec in refspecs}
        if not refspecs and working_branch:
            targets = {working_branch}
        hit = sorted(targets & protected)
        if hit:
            return deny(f"'{segment.strip()}' pushes to protected branch {', '.join(hit)}.")
        if flags & FORCE_FLAGS or any(refspec.startswith("+") for refspec in refspecs):
            return {"decision": "force_ask", "reason": f"Force push: '{segment.strip()}'. Confirm it is intended."}
    return {"decision": "allow"}


def deny(reason: str) -> dict:
    return {
        "decision": "deny",
        "reason": f"{reason} Open a branch and a pull request instead; a person pushes protected branches.",
    }


def main() -> None:
    try:
        payload = json.loads(sys.stdin.read() or "{}")
        result = decide(payload)
    except Exception:
        result = {"decision": "allow"}
    print(json.dumps(result))


if __name__ == "__main__":
    main()
