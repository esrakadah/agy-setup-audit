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
SEGMENT_SEPARATOR = re.compile(r"&&|\|\||;|\||\n|\(|\)")
ENV_ASSIGNMENT_PATTERN = re.compile(r"^[A-Za-z_]\w*=.*$")
WRAPPER_BINARIES = {"env", "command", "sudo"}
SHELL_BINARIES = {"sh", "bash", "zsh"}
FORCE_FLAGS = {"-f", "--force", "--force-with-lease", "--force-if-includes"}
DELETE_FLAGS = {"-d", "--delete"}
PUSH_EVERYTHING_FLAGS = {"--all", "--mirror"}
GIT_OPTIONS_WITH_VALUE = {"-C", "-c", "--git-dir", "--work-tree", "--namespace"}
PUSH_OPTIONS_WITH_VALUE = {"-o", "--push-option", "--repo", "--receive-pack", "--exec"}


def protected_branches() -> set[str]:
    configured = os.environ.get("AGY_PROTECTED_BRANCHES", DEFAULT_PROTECTED_BRANCHES)
    return {branch.strip() for branch in configured.split(",") if branch.strip()}


def strip_command_wrappers(tokens: list[str]) -> list[str]:
    index = 0
    while index < len(tokens):
        token = tokens[index]
        base = os.path.basename(token)
        if ENV_ASSIGNMENT_PATTERN.match(token) or base in WRAPPER_BINARIES:
            index += 1
            continue
        break
    return tokens[index:]


def expand_shell_segments(command_line: str) -> list[str]:
    segments: list[str] = []
    for raw_segment in SEGMENT_SEPARATOR.split(command_line):
        segment = raw_segment.strip()
        if not segment:
            continue
        try:
            tokens = strip_command_wrappers(shlex.split(segment))
        except ValueError:
            segments.append(segment)
            continue
        if len(tokens) >= 3 and os.path.basename(tokens[0]) in SHELL_BINARIES and tokens[1] in ("-c", "-lc"):
            segments.extend(expand_shell_segments(tokens[2]))
        else:
            segments.append(segment)
    return segments


def push_arguments(segment: str) -> tuple[list[str], str | None] | None:
    try:
        tokens = strip_command_wrappers(shlex.split(segment))
    except ValueError:
        return None
    if not tokens or os.path.basename(tokens[0]) != "git":
        return None
    git_dir = None
    index = 1
    while index < len(tokens) and tokens[index].startswith("-"):
        token = tokens[index]
        if token in GIT_OPTIONS_WITH_VALUE and index + 1 < len(tokens):
            if token == "-C":
                git_dir = tokens[index + 1]
            index += 2
        elif token.startswith("-C") and len(token) > 2:
            git_dir = token[2:]
            index += 1
        elif token.startswith("--git-dir="):
            git_dir = token.split("=", 1)[1]
            index += 1
        else:
            index += 1
    if index >= len(tokens) or tokens[index] != "push":
        return None
    return tokens[index + 1 :], git_dir


def split_push_flags_and_positionals(push_tokens: list[str]) -> tuple[list[str], list[str], bool]:
    flags: list[str] = []
    positional: list[str] = []
    has_repo_flag = False
    index = 0
    end_of_options = False
    while index < len(push_tokens):
        token = push_tokens[index]
        if end_of_options:
            positional.append(token)
            index += 1
            continue
        if token == "--":
            end_of_options = True
            index += 1
            continue
        if token in PUSH_OPTIONS_WITH_VALUE and index + 1 < len(push_tokens):
            if token == "--repo":
                has_repo_flag = True
            flags.append(token)
            index += 2
            continue
        if token.startswith("-"):
            if token.startswith("--repo="):
                has_repo_flag = True
            flags.append(token)
            index += 1
            continue
        positional.append(token)
        index += 1
    return flags, positional, has_repo_flag


def has_force_flag(flags: list[str]) -> bool:
    for token in flags:
        if token in FORCE_FLAGS or token.startswith(("--force-with-lease=", "--force-if-includes=")):
            return True
        if token.startswith("-") and not token.startswith("--") and "f" in token[1:]:
            return True
    return False


def has_delete_flag(flags: list[str]) -> bool:
    for token in flags:
        if token in DELETE_FLAGS:
            return True
        if token.startswith("-") and not token.startswith("--") and "d" in token[1:]:
            return True
    return False


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

    for segment in expand_shell_segments(command_line):
        parsed = push_arguments(segment)
        if parsed is None:
            continue
        push_tokens, git_dir = parsed
        working_branch = current_branch(git_dir or arguments.get("Cwd") or workspace_paths[0])
        flags, positional, has_repo_flag = split_push_flags_and_positionals(push_tokens)
        flag_set = set(flags)
        refspecs = positional if has_repo_flag else positional[1:]

        if flag_set & PUSH_EVERYTHING_FLAGS:
            return deny(f"'{segment}' pushes every branch, including protected ones.")
        targets = {destination_branch(refspec, working_branch) for refspec in refspecs}
        if not refspecs and working_branch:
            targets = {working_branch}
        hit = sorted(targets & protected)
        if hit:
            action = "deletes" if has_delete_flag(flags) or any(refspec.startswith(":") for refspec in refspecs) else "pushes to"
            return deny(f"'{segment}' {action} protected branch {', '.join(hit)}.")
        if has_force_flag(flags) or any(refspec.startswith("+") for refspec in refspecs):
            return {"decision": "force_ask", "reason": f"Force push: '{segment}'. Confirm it is intended."}
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
