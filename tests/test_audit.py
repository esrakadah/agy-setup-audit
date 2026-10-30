from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
AUDIT_SCRIPT = REPO_ROOT / "skills" / "agy-setup-audit" / "scripts" / "agy_audit.py"
BROKEN_LAB = REPO_ROOT / "labs" / "broken-workspace"
EMPTY_GEMINI_HOME = REPO_ROOT / "labs" / "empty-gemini-home"
TEMPLATES_DIR = REPO_ROOT / "templates"


def load_audit_module():
    spec = importlib.util.spec_from_file_location("agy_audit", AUDIT_SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules["agy_audit"] = module
    spec.loader.exec_module(module)
    return module


agy_audit = load_audit_module()


class AuditScriptTest(unittest.TestCase):
    def test_broken_workspace_lab_catches_every_planted_finding(self) -> None:
        findings = agy_audit.Audit(BROKEN_LAB, EMPTY_GEMINI_HOME).run()
        errors = [finding for finding in findings if finding.severity == agy_audit.ERROR]
        warnings = [finding for finding in findings if finding.severity == agy_audit.WARN]
        notes = [finding for finding in findings if finding.severity == agy_audit.INFO]

        self.assertEqual(len(errors), 7)
        self.assertEqual(len(warnings), 6)
        self.assertEqual(len(notes), 1)
        self.assertTrue(all(not finding.path.startswith("/") for finding in findings))

        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            exit_code = agy_audit.main(
                ["--workspace", str(BROKEN_LAB), "--gemini-home", str(EMPTY_GEMINI_HOME), "--json"]
            )
        self.assertEqual(exit_code, 1)
        self.assertEqual(len(json.loads(buffer.getvalue())), 14)

    def test_plugin_repo_root_audits_itself_with_zero_findings(self) -> None:
        findings = agy_audit.Audit(REPO_ROOT, EMPTY_GEMINI_HOME).run()
        self.assertEqual(findings, [])

    def test_rule_byte_cap_include_expansion_and_aggregate_budget(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = Path(temp_dir) / "workspace"
            gemini_home = Path(temp_dir) / "gemini"
            rules_dir = workspace / ".agents" / "rules"
            rules_dir.mkdir(parents=True)
            gemini_home.mkdir()

            included = workspace / "extra.md"
            included.write_text("x" * 20_000, encoding="utf-8")
            agents_md = workspace / "AGENTS.md"
            agents_md.write_text(
                "---\ntrigger: always_on\n---\n# Rules\n@[extra](/extra.md)\n" + ("y" * 5_000),
                encoding="utf-8",
            )
            for idx in range(3):
                (rules_dir / f"always-{idx}.md").write_text(
                    "---\ntrigger: always_on\n---\n" + ("z" * 20_000),
                    encoding="utf-8",
                )

            findings = agy_audit.Audit(workspace, gemini_home).run()
            rule_findings = [finding for finding in findings if finding.check == "rules"]
            self.assertTrue(any(finding.severity == agy_audit.ERROR and "drops the last" in finding.message for finding in rule_findings))
            self.assertTrue(any(finding.severity == agy_audit.WARN and "does not support frontmatter" in finding.message for finding in rule_findings))
            self.assertTrue(any(finding.severity == agy_audit.WARN and "aggregate budget" in finding.message for finding in rule_findings))

    def test_multiline_block_scalar_and_inline_tools_list(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = Path(temp_dir) / "workspace"
            gemini_home = Path(temp_dir) / "gemini"
            agents_dir = workspace / ".agents" / "agents"
            agents_dir.mkdir(parents=True)
            gemini_home.mkdir()

            (agents_dir / "helper.md").write_text(
                "---\nname: helper\ndescription: >-\n  First line of text\n  second line says code reviewer\n"
                "tools: [view_file, write_file]\n---\n",
                encoding="utf-8",
            )

            findings = agy_audit.Audit(workspace, gemini_home).run()
            agent_messages = [finding.message for finding in findings if finding.check == "agents"]
            self.assertTrue(any("Reviewer agent can change files: write_file" in msg for msg in agent_messages))

    def test_skills_inherits_dot_path_direct_folder_and_missing_name(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = Path(temp_dir) / "workspace"
            gemini_home = Path(temp_dir) / "gemini"
            (workspace / ".git").mkdir(parents=True)
            agents_dir = workspace / ".agents"
            (agents_dir / "skills" / "bad-name").mkdir(parents=True)
            gemini_home.mkdir()

            (agents_dir / "skills" / "bad-name" / "SKILL.md").write_text(
                "---\nname: Bad Name\ndescription: Has spaces.\n---\n",
                encoding="utf-8",
            )
            shared_skill = workspace / "shared-skills" / "my-skill"
            shared_skill.mkdir(parents=True)
            (shared_skill / "SKILL.md").write_text(
                "---\ndescription: Missing name field.\n---\n",
                encoding="utf-8",
            )
            shared_manifest = workspace / "shared-skills.json"
            shared_manifest.write_text(
                json.dumps({"entries": [{"path": "shared-skills"}]}),
                encoding="utf-8",
            )
            (agents_dir / "skills.json").write_text(
                json.dumps(
                    {
                        "inherits": [{"path": "shared-skills.json"}],
                        "entries": [{"path": "."}, {"path": "shared-skills/my-skill"}],
                    }
                ),
                encoding="utf-8",
            )

            findings = agy_audit.Audit(workspace, gemini_home).run()
            skill_messages = [finding.message for finding in findings if finding.check == "skills"]
            self.assertTrue(any("resolves to the project root" in msg for msg in skill_messages))
            self.assertTrue(any("points directly at skill folder" in msg for msg in skill_messages))
            self.assertTrue(any("Frontmatter has no 'name'" in msg for msg in skill_messages))
            self.assertTrue(any("not lowercase kebab-case" in msg for msg in skill_messages))
            self.assertFalse(any("outside every discovery path" in msg for msg in skill_messages))

    def test_plugins_jsonc_disabled_skip_logo_and_duplicate_name_check(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = Path(temp_dir) / "workspace"
            gemini_home = Path(temp_dir) / "gemini"
            plugins_dir = workspace / ".agents" / "plugins"
            (plugins_dir / "alpha").mkdir(parents=True)
            (plugins_dir / "beta").mkdir(parents=True)
            (plugins_dir / "gamma").mkdir(parents=True)
            (plugins_dir / "disabled-one").mkdir(parents=True)
            gemini_home.mkdir()

            (plugins_dir / "alpha" / "plugin.json").write_text(
                '{\n  // comment allowed in JSONC\n  "name": "dup-plugin",\n  "logo": "https://example.com/logo.png",\n}\n',
                encoding="utf-8",
            )
            (plugins_dir / "beta" / "plugin.json").write_text(
                json.dumps(
                    {
                        "name": "dup-plugin",
                        "logo": "assets/missing.svg",
                        "extraKey": True,
                        "suggestedPrompts": ["1", "2", "3", "4"],
                    }
                ),
                encoding="utf-8",
            )
            (plugins_dir / "gamma" / "plugin.json").write_text(
                json.dumps({"name": "gamma-plugin", "logo": "assets/logo.gif"}),
                encoding="utf-8",
            )
            (plugins_dir / "disabled-one" / "plugin.json").write_text(
                json.dumps({"name": "dup-plugin", "disabled": True}),
                encoding="utf-8",
            )
            (plugins_dir / "disabled-one" / "hooks").mkdir()
            (plugins_dir / "disabled-one" / "hooks" / "hooks.json").write_text("{}", encoding="utf-8")

            findings = agy_audit.Audit(workspace, gemini_home).run()
            plugin_findings = [finding for finding in findings if finding.check == "plugins"]
            self.assertTrue(any(f.severity == agy_audit.ERROR and "collides" in f.message for f in plugin_findings))
            self.assertTrue(any("URL or absolute path" in f.message for f in plugin_findings))
            self.assertTrue(any("does not exist" in f.message for f in plugin_findings))
            self.assertTrue(any("supported image extension" in f.message for f in plugin_findings))
            self.assertTrue(any("suggestedPrompts has 4" in f.message for f in plugin_findings))
            self.assertTrue(any("Unknown top-level field(s)" in f.message for f in plugin_findings))
            hook_findings = [finding for finding in findings if finding.check == "hooks" and finding.severity == agy_audit.ERROR]
            self.assertEqual(hook_findings, [])

    def test_hooks_handler_type_and_matcher_validation(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = Path(temp_dir) / "workspace"
            gemini_home = Path(temp_dir) / "gemini"
            agents_dir = workspace / ".agents"
            agents_dir.mkdir(parents=True)
            gemini_home.mkdir()

            (agents_dir / "hooks.json").write_text(
                json.dumps(
                    {
                        "bad-hook": {
                            "PreToolUse": [
                                {
                                    "matcher": "CORTEX_STEP_TYPE_RUN_COMMAND",
                                    "hooks": [{"type": "prompt", "command": "echo hi"}],
                                },
                                {
                                    "matcher": "[unclosed",
                                    "hooks": [{"type": "command", "command": "echo hi"}],
                                },
                            ]
                        }
                    }
                ),
                encoding="utf-8",
            )

            findings = agy_audit.Audit(workspace, gemini_home).run()
            hook_messages = [finding.message for finding in findings if finding.check == "hooks"]
            self.assertTrue(any("unsupported handler type 'prompt'" in msg for msg in hook_messages))
            self.assertTrue(any("raw 'CORTEX_STEP_TYPE_' prefix" in msg for msg in hook_messages))
            self.assertTrue(any("not a valid regex" in msg for msg in hook_messages))

    def test_mcp_cache_sse_and_duplicate_server_name_detection(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = Path(temp_dir) / "workspace"
            gemini_home = Path(temp_dir) / "gemini"
            (workspace / ".agents").mkdir(parents=True)
            (workspace / "AGENTS.md").write_text("# Rules\n", encoding="utf-8")
            (workspace / ".agents" / "mcp_config.json").write_text(
                json.dumps(
                    {
                        "mcpServers": {
                            "legacy-remote": {"url": "https://mcp.example.com/sse"},
                            "shared-name": {"command": "node", "args": ["a.js"]},
                        }
                    }
                ),
                encoding="utf-8",
            )

            plugin_dir = gemini_home / "config" / "plugins" / "flutter"
            plugin_dir.mkdir(parents=True)
            (plugin_dir / "plugin.json").write_text(json.dumps({"name": "flutter"}), encoding="utf-8")
            (plugin_dir / "mcp_config.json").write_text(
                json.dumps(
                    {
                        "mcpServers": {
                            "dart-mcp-server": {"command": "dart", "args": ["mcp-server"]},
                            "shared-name": {"command": "node", "args": ["b.js"]},
                        }
                    }
                ),
                encoding="utf-8",
            )

            cache_root = gemini_home / "antigravity" / "mcp"
            (cache_root / "flutter_dart-mcp-server").mkdir(parents=True)
            (cache_root / "removed-server").mkdir(parents=True)

            findings = agy_audit.Audit(workspace, gemini_home).run()
            mcp_findings = [finding for finding in findings if finding.check == "mcp"]
            self.assertTrue(any(f.severity == agy_audit.ERROR and "/sse" in f.message for f in mcp_findings))
            self.assertTrue(any(f.severity == agy_audit.WARN and "shared-name" in f.message for f in mcp_findings))
            self.assertTrue(any(f.severity == agy_audit.WARN and "removed-server" in f.path for f in mcp_findings))

    def test_templates_pass_audit_with_zero_errors_and_warnings(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = Path(temp_dir) / "workspace"
            gemini_home = Path(temp_dir) / "gemini"
            agents_root = workspace / ".agents"
            gemini_home.mkdir()

            shutil.copytree(TEMPLATES_DIR / "rules", agents_root / "rules")
            shutil.copytree(TEMPLATES_DIR / "skills", agents_root / "skills")
            shutil.copytree(TEMPLATES_DIR / "agents", agents_root / "agents")
            shutil.copytree(TEMPLATES_DIR / "style-gate", agents_root / "style-gate")
            shutil.copy(TEMPLATES_DIR / "style-gate" / "hooks.json", agents_root / "hooks.json")
            (workspace / "AGENTS.md").write_text("# Rules\n- Keep rules short.\n", encoding="utf-8")

            findings = agy_audit.Audit(workspace, gemini_home).run()
            problems = [finding for finding in findings if finding.severity in (agy_audit.ERROR, agy_audit.WARN)]
            self.assertEqual(problems, [])


if __name__ == "__main__":
    unittest.main()
