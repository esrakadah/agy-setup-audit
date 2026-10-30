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
        self.assertEqual(len(warnings), 5)
        self.assertEqual(len(notes), 1)
        self.assertTrue(all(not finding.path.startswith("/") for finding in findings))

        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            exit_code = agy_audit.main(
                ["--workspace", str(BROKEN_LAB), "--gemini-home", str(EMPTY_GEMINI_HOME), "--json"]
            )
        self.assertEqual(exit_code, 1)
        self.assertEqual(len(json.loads(buffer.getvalue())), 13)

    def test_rule_byte_cap_include_expansion_and_frontmatter_warning(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = Path(temp_dir) / "workspace"
            gemini_home = Path(temp_dir) / "gemini"
            workspace.mkdir()
            gemini_home.mkdir()

            included = workspace / "extra.md"
            included.write_text("x" * 20_000, encoding="utf-8")
            agents_md = workspace / "AGENTS.md"
            agents_md.write_text(
                "---\ntrigger: always_on\n---\n# Rules\n@[extra](extra.md)\n" + ("y" * 5_000),
                encoding="utf-8",
            )

            findings = agy_audit.Audit(workspace, gemini_home).run()
            rule_findings = [finding for finding in findings if finding.check == "rules"]
            self.assertTrue(any(finding.severity == agy_audit.ERROR and "drops the last" in finding.message for finding in rule_findings))
            self.assertTrue(any(finding.severity == agy_audit.WARN and "does not support frontmatter" in finding.message for finding in rule_findings))

    def test_mcp_cache_detection_and_plugin_namespace_matching(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = Path(temp_dir) / "workspace"
            gemini_home = Path(temp_dir) / "gemini"
            (workspace / ".agents").mkdir(parents=True)
            (workspace / "AGENTS.md").write_text("# Rules\n", encoding="utf-8")

            plugin_dir = gemini_home / "config" / "plugins" / "flutter"
            plugin_dir.mkdir(parents=True)
            (plugin_dir / "plugin.json").write_text(json.dumps({"name": "flutter"}), encoding="utf-8")
            (plugin_dir / "mcp_config.json").write_text(
                json.dumps({"mcpServers": {"dart-mcp-server": {"command": "dart", "args": ["mcp-server"]}}}),
                encoding="utf-8",
            )

            cache_root = gemini_home / "antigravity" / "mcp"
            (cache_root / "flutter_dart-mcp-server").mkdir(parents=True)
            (cache_root / "removed-server").mkdir(parents=True)

            findings = agy_audit.Audit(workspace, gemini_home).run()
            mcp_warnings = [finding for finding in findings if finding.check == "mcp" and finding.severity == agy_audit.WARN]
            self.assertEqual(len(mcp_warnings), 1)
            self.assertIn("removed-server", mcp_warnings[0].path)

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
