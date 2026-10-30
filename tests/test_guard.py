from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
GUARD_SCRIPT = REPO_ROOT / "scripts" / "guard_protected_push.py"
STYLE_GATE_SCRIPT = REPO_ROOT / "templates" / "style-gate" / "stop_style_gate.py"


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


guard = load_module("guard_protected_push", GUARD_SCRIPT)
style_gate = load_module("stop_style_gate", STYLE_GATE_SCRIPT)


def command_payload(command_line: str, workspace: str = "/repo") -> dict:
    return {
        "workspacePaths": [workspace],
        "toolCall": {
            "name": "run_command",
            "args": {"CommandLine": command_line},
        },
    }


class ProtectedPushGuardTest(unittest.TestCase):
    def test_allows_non_command_tools_and_safe_git_commands(self) -> None:
        self.assertEqual(guard.decide({"toolCall": {"name": "view_file"}}), {"decision": "allow"})
        with mock.patch.object(guard, "current_branch", return_value="main"):
            self.assertEqual(guard.decide(command_payload("git status --short")), {"decision": "allow"})
            self.assertEqual(guard.decide(command_payload("git push origin feature/audit")), {"decision": "allow"})
            self.assertEqual(guard.decide(command_payload("git push --delete origin feature/old")), {"decision": "allow"})

    def test_denies_explicit_push_and_delete_on_protected_branch(self) -> None:
        with mock.patch.object(guard, "current_branch", return_value="feature/audit"):
            for command in (
                "git push origin main",
                "git push origin master",
                "git push origin HEAD:main",
                "git push origin +HEAD:refs/heads/main",
                "pytest && git push -u origin main",
                "GITLAB_HOST=gitlab.chrg.it git push origin main",
                "env git -C/tmp/repo push origin main",
                "git push --repo=origin main",
                "git push --repo origin main",
                "(cd /tmp/repo && git push origin main)",
                'sh -c "git push origin main"',
                "git push --delete origin main",
                "git push origin :main",
            ):
                with self.subTest(command=command):
                    decision = guard.decide(command_payload(command))
                    self.assertEqual(decision["decision"], "deny")
                    self.assertIn("protected branch", decision["reason"])

    def test_denies_bare_push_and_push_options_when_checked_out_on_main(self) -> None:
        with mock.patch.object(guard, "current_branch", return_value="main"):
            self.assertEqual(guard.decide(command_payload("git push"))["decision"], "deny")
            self.assertEqual(guard.decide(command_payload("git push origin HEAD"))["decision"], "deny")
            self.assertEqual(guard.decide(command_payload("git push -o ci.skip origin"))["decision"], "deny")

    def test_allows_bare_push_on_feature_branch(self) -> None:
        with mock.patch.object(guard, "current_branch", return_value="feature/audit"):
            self.assertEqual(guard.decide(command_payload("git push")), {"decision": "allow"})

    def test_denies_push_all_and_mirror(self) -> None:
        with mock.patch.object(guard, "current_branch", return_value="feature/audit"):
            self.assertEqual(guard.decide(command_payload("git push --all origin"))["decision"], "deny")
            self.assertEqual(guard.decide(command_payload("git push --mirror origin"))["decision"], "deny")

    def test_asks_on_force_push_to_feature_branch(self) -> None:
        with mock.patch.object(guard, "current_branch", return_value="feature/audit"):
            for command in (
                "git push --force-with-lease origin feature/audit",
                "git push --force-with-lease=feature/audit:abc123 origin feature/audit",
                "git push -f origin feature/audit",
                "git push -uf origin feature/audit",
                "git push origin +feature/audit",
            ):
                with self.subTest(command=command):
                    decision = guard.decide(command_payload(command))
                    self.assertEqual(decision["decision"], "force_ask")

    def test_respects_custom_protected_branches_env(self) -> None:
        with mock.patch.dict(os.environ, {"AGY_PROTECTED_BRANCHES": "production, release"}):
            with mock.patch.object(guard, "current_branch", return_value="feature/audit"):
                self.assertEqual(guard.decide(command_payload("git push origin production"))["decision"], "deny")
                self.assertEqual(guard.decide(command_payload("git push origin main"))["decision"], "allow")

    def test_cli_fails_open_on_invalid_json(self) -> None:
        result = subprocess.run(
            [sys.executable, str(GUARD_SCRIPT)],
            input="{not-json",
            capture_output=True,
            text=True,
            check=True,
        )
        self.assertEqual(json.loads(result.stdout), {"decision": "allow"})


class StopStyleGateTest(unittest.TestCase):
    def write_transcript(self, directory: Path, content: str) -> Path:
        transcript = directory / "transcript.jsonl"
        step = {"type": "PLANNER_RESPONSE", "content": content}
        transcript.write_text(json.dumps(step) + "\n", encoding="utf-8")
        return transcript

    def test_flags_em_dash_and_inflected_banned_words_on_normal_stop(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            for sample in (
                "We are delving into the configuration.",
                "These synergies across teams help.",
                "Rich tapestries of rules.",
                "Bridging the gap between tools.",
                "One rule \u2014 never truncate.",
            ):
                with self.subTest(sample=sample):
                    transcript = self.write_transcript(Path(temp_dir), sample)
                    decision = style_gate.decide(
                        {"executionNum": 1, "terminationReason": "NO_TOOL_CALL", "transcriptPath": str(transcript)}
                    )
                    self.assertEqual(decision["decision"], "continue")

    def test_ignores_code_blocks_second_execution_and_user_cancel(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            transcript = self.write_transcript(
                Path(temp_dir),
                "Clean prose with `delve` in inline code and:\n```python\n# seamless \u2014 comment\n```\n",
            )
            self.assertEqual(
                style_gate.decide({"executionNum": 1, "transcriptPath": str(transcript)}),
                {"decision": "allow"},
            )

            flagged = self.write_transcript(Path(temp_dir), "Let us delve in.")
            self.assertEqual(
                style_gate.decide({"executionNum": 2, "transcriptPath": str(flagged)}),
                {"decision": "allow"},
            )
            self.assertEqual(
                style_gate.decide(
                    {"executionNum": 1, "terminationReason": "USER_CANCELED", "transcriptPath": str(flagged)}
                ),
                {"decision": "allow"},
            )


if __name__ == "__main__":
    unittest.main()
