#!/usr/bin/env python3
"""Read-only audit of a Google Antigravity setup.

Finds the failures Antigravity does not report: rule files cut at the 24,000-byte cap or
exceeding the 20,000-token aggregate budget, skills that never load, MCP servers configured
twice, using legacy /sse URLs, or cached after removal, plugins with duplicate names, hooks
that cannot run, and reviewer agents that can write files.

Never writes, moves or deletes anything. Standard library only (Python 3.9+).
"""
from __future__ import annotations

import argparse
import fnmatch
import json
import os
import re
import shlex
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

RULE_FILE_CAP_BYTES = 24_000
RULE_FILE_WARN_RATIO = 0.8
AGGREGATE_RULE_BUDGET_TOKENS = 20_000
BYTES_PER_TOKEN_ESTIMATE = 4
AGGREGATE_RULE_BUDGET_BYTES = AGGREGATE_RULE_BUDGET_TOKENS * BYTES_PER_TOKEN_ESTIMATE
MIN_SECRET_LENGTH = 8
MAX_SUGGESTED_PROMPTS = 3
DOT_DIR_NAMES = (".agents", ".agent", "_agents", "_agent")
STANDALONE_RULE_NAMES = ("GEMINI.md", "AGENTS.md")
SKIPPED_DIR_NAMES = {".git", "node_modules", ".dart_tool", "build", ".venv", "__pycache__"}
FLAT_HOOK_EVENTS = {"SessionStart", "PreInvocation", "PostInvocation", "Stop"}
GROUPED_HOOK_EVENTS = {"PreToolUse", "PostToolUse"}
HOOK_SPEC_KEYS = FLAT_HOOK_EVENTS | GROUPED_HOOK_EVENTS | {"enabled"}
PRODUCT_DIR_NAMES = ("antigravity", "antigravity-ide", "antigravity-cli")
LOGO_EXTENSIONS = (".png", ".svg", ".jpg", ".jpeg", ".webp")
PLUGIN_MANIFEST_KEYS = {
    "name",
    "description",
    "logo",
    "suggestedPrompts",
    "displayName",
    "version",
    "author",
    "homepage",
    "repository",
    "disabled",
}
WRITE_TOOL_NAMES = {
    "write_to_file",
    "replace_file_content",
    "multi_replace_file_content",
    "run_command",
    "delete_file",
    "write_file",
    "edit_file",
    "bash",
    "generate_image",
}
KNOWN_TOOL_NAMES = WRITE_TOOL_NAMES | {
    "view_file",
    "view_file_outline",
    "view_code_item",
    "list_dir",
    "grep_search",
    "find_by_name",
    "read_file",
    "search_web",
    "read_url_content",
    "read_browser_page",
    "read_terminal",
    "command_status",
    "send_message",
    "schedule",
    "invoke_subagent",
    "define_subagent",
    "manage_subagents",
    "manage_task",
    "ask_question",
    "call_mcp_tool",
    "list_resources",
    "read_resource",
    "list_plugin_accounts",
    "search_marketplace",
}
RULE_TRIGGERS = {"always_on", "model_decision", "glob", "manual"}
AGENT_MODELS = {"inherit", "flash", "flash_lite", "pro"}
SKILL_NAME_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
REVIEWER_PATTERN = re.compile(r"review|audit|critic|lint|verif|inspect", re.IGNORECASE)
SECRET_KEY_PATTERN = re.compile(r"key|token|secret|password|passwd|pat\b|credential", re.IGNORECASE)
INCLUDE_PATTERN = re.compile(r"@\[[^\]]*\]\(([^)]+)\)")
SCRIPT_TOKEN_PATTERN = re.compile(r"\.(py|sh|js|mjs|ts|rb)$")
JSONC_TOKEN_PATTERN = re.compile(r'"(?:\\.|[^"\\])*"|//[^\n]*|/\*.*?\*/', re.DOTALL)
TRAILING_COMMA_PATTERN = re.compile(r",(\s*[}\]])")

ERROR = "ERROR"
WARN = "WARN"
INFO = "INFO"
SEVERITY_ORDER = {ERROR: 0, WARN: 1, INFO: 2}


@dataclass
class Finding:
    check: str
    severity: str
    path: str
    message: str
    fix: str


@dataclass
class Plugin:
    name: str
    directory: Path
    manifest_path: Path
    manifest: dict
    enabled: bool
    is_workspace: bool


class Audit:
    def __init__(self, workspace: Path, gemini_home: Path):
        self.workspace = workspace.resolve()
        self.gemini_home = gemini_home.expanduser()
        self.global_root = self.gemini_home / "config"
        self.repo_root = find_repo_root(self.workspace)
        self.findings: list[Finding] = []
        self._all_plugins: list[Plugin] | None = None

    def short_path(self, path: Path | str) -> str:
        if isinstance(path, Path):
            try:
                return str(path.resolve().relative_to(self.workspace))
            except ValueError:
                pass
        return display_path(path)

    def report(self, check: str, severity: str, path: Path | str, message: str, fix: str) -> None:
        self.findings.append(Finding(check, severity, self.short_path(path), message, fix))

    def run(self) -> list[Finding]:
        self.check_plugins()
        self.check_rules()
        self.check_skills()
        self.check_mcp()
        self.check_hooks()
        self.check_agents()
        return sorted(self.findings, key=lambda finding: (finding.check, SEVERITY_ORDER[finding.severity], finding.path))

    def workspace_roots(self) -> list[Path]:
        roots = []
        directory = self.workspace
        while True:
            for dot_dir_name in DOT_DIR_NAMES:
                candidate = directory / dot_dir_name
                if candidate.is_dir():
                    roots.append(candidate)
            if directory == self.repo_root or directory.parent == directory:
                break
            directory = directory.parent
        return roots

    def customization_roots(self) -> list[Path]:
        roots = self.workspace_roots()
        if self.global_root.is_dir():
            roots.append(self.global_root)
        return roots

    def user_plugin_states(self) -> dict[str, bool]:
        config_path = self.global_root / "config.json"
        if not config_path.is_file() or not os.access(config_path, os.R_OK):
            return {}
        config = read_json(config_path) or {}
        plugins_map = config.get("plugins")
        if not isinstance(plugins_map, dict):
            return {}
        return {
            dir_name: bool(entry.get("enabled", True))
            for dir_name, entry in plugins_map.items()
            if isinstance(entry, dict) and "enabled" in entry
        }

    def all_plugins(self) -> list[Plugin]:
        if self._all_plugins is not None:
            return self._all_plugins
        user_states = self.user_plugin_states()
        workspace_root_set = {root.resolve() for root in self.workspace_roots()}
        candidate_dirs: list[tuple[Path, bool]] = []
        seen_dirs: set[Path] = set()

        if plugin_manifest_path(self.workspace) is not None:
            seen_dirs.add(self.workspace)
            candidate_dirs.append((self.workspace, True))

        for root in self.customization_roots():
            is_ws = root.resolve() in workspace_root_set
            plugins_dir = root / "plugins"
            if plugins_dir.is_dir():
                for plugin_dir in sorted(path for path in plugins_dir.iterdir() if path.is_dir()):
                    resolved = plugin_dir.resolve()
                    if resolved not in seen_dirs:
                        seen_dirs.add(resolved)
                        candidate_dirs.append((plugin_dir, is_ws))
            plugins_manifest = root / "plugins.json"
            if plugins_manifest.is_file():
                for plugin_dir in self.expand_manifest_dirs(plugins_manifest, root, "plugins"):
                    resolved = plugin_dir.resolve()
                    if resolved not in seen_dirs:
                        seen_dirs.add(resolved)
                        candidate_dirs.append((plugin_dir, is_ws))

        found: list[Plugin] = []
        for plugin_dir, is_ws in candidate_dirs:
            manifest_path = plugin_manifest_path(plugin_dir)
            if manifest_path is None:
                continue
            manifest = read_jsonc(manifest_path)
            if manifest is None:
                self.report(
                    "plugins",
                    ERROR,
                    manifest_path,
                    f"{manifest_path.name} is not valid JSON/JSONC.",
                    "Fix the JSON syntax in plugin.json.",
                )
                manifest = {}
            name = manifest.get("name") or plugin_dir.name
            default_enabled = not bool(manifest.get("disabled", False))
            enabled = user_states.get(plugin_dir.name, default_enabled)
            found.append(
                Plugin(
                    name=name,
                    directory=plugin_dir,
                    manifest_path=manifest_path,
                    manifest=manifest,
                    enabled=enabled,
                    is_workspace=is_ws,
                )
            )
        self._all_plugins = found
        return found

    def plugins(self) -> list[Plugin]:
        return [plugin for plugin in self.all_plugins() if plugin.enabled]

    def check_plugins(self) -> None:
        names_seen: dict[str, Path] = {}
        for plugin in self.all_plugins():
            if plugin.enabled:
                if plugin.name in names_seen and names_seen[plugin.name] != plugin.directory.resolve():
                    self.report(
                        "plugins",
                        ERROR,
                        plugin.manifest_path,
                        f"Plugin name '{plugin.name}' collides with {self.short_path(names_seen[plugin.name])}; "
                        "names share one global namespace and the second plugin is dropped silently.",
                        "Give each plugin a unique lowercase kebab-case 'name' in plugin.json.",
                    )
                names_seen.setdefault(plugin.name, plugin.directory.resolve())

            if not plugin.is_workspace:
                continue
            logo = plugin.manifest.get("logo")
            if isinstance(logo, str):
                if logo.startswith(("http://", "https://", "/")):
                    self.report(
                        "plugins",
                        WARN,
                        plugin.manifest_path,
                        f"logo '{logo}' uses a URL or absolute path; Antigravity rejects it silently and renders no logo.",
                        "Use a path relative to the plugin root, such as 'assets/logo.svg'.",
                    )
                elif not logo.lower().endswith(LOGO_EXTENSIONS):
                    self.report(
                        "plugins",
                        WARN,
                        plugin.manifest_path,
                        f"logo '{logo}' does not end in a supported image extension ({', '.join(LOGO_EXTENSIONS)}).",
                        "Use a .png, .svg, .jpg, .jpeg, or .webp asset relative to the plugin root.",
                    )
                elif not (plugin.directory / logo).is_file():
                    self.report(
                        "plugins",
                        WARN,
                        plugin.manifest_path,
                        f"logo '{logo}' does not exist at {self.short_path(plugin.directory / logo)}.",
                        "Create the logo file or remove the 'logo' field from plugin.json.",
                    )
            prompts = plugin.manifest.get("suggestedPrompts")
            if isinstance(prompts, list) and len(prompts) > MAX_SUGGESTED_PROMPTS:
                self.report(
                    "plugins",
                    WARN,
                    plugin.manifest_path,
                    f"suggestedPrompts has {len(prompts)} entries; prompts beyond {MAX_SUGGESTED_PROMPTS} are ignored.",
                    f"Keep at most {MAX_SUGGESTED_PROMPTS} starter prompts.",
                )
            unknown_keys = sorted(set(plugin.manifest.keys()) - PLUGIN_MANIFEST_KEYS)
            if unknown_keys:
                self.report(
                    "plugins",
                    WARN,
                    plugin.manifest_path,
                    f"Unknown top-level field(s) in plugin.json: {', '.join(unknown_keys)}; they are silently discarded.",
                    f"Use only documented fields: {', '.join(sorted(PLUGIN_MANIFEST_KEYS))}.",
                )

    def check_rules(self) -> None:
        rule_files: dict[Path, tuple[Path, bool]] = {}
        standalone_dirs = []
        directory = self.workspace
        while True:
            standalone_dirs.append(directory)
            standalone_dirs += [directory / dot_dir_name for dot_dir_name in DOT_DIR_NAMES]
            if directory == self.repo_root or directory.parent == directory:
                break
            directory = directory.parent
        standalone_dirs += [self.gemini_home, self.global_root]
        for standalone_dir in standalone_dirs:
            for rule_name in STANDALONE_RULE_NAMES:
                candidate = standalone_dir / rule_name
                if candidate.is_file():
                    rule_files.setdefault(candidate.resolve(), (candidate, True))

        triggered_rule_dirs = [root / "rules" for root in self.customization_roots()]
        triggered_rule_dirs.append(self.gemini_home / "antigravity-cli" / "rules")
        plugin_rule_dirs = [plugin.directory / "rules" for plugin in self.plugins()]
        for rule_dir in triggered_rule_dirs + plugin_rule_dirs:
            if not rule_dir.is_dir():
                continue
            for candidate in sorted(rule_dir.glob("*.md")):
                is_always_on = candidate.name in STANDALONE_RULE_NAMES
                if candidate.name not in STANDALONE_RULE_NAMES:
                    trigger = self.check_rule_trigger(candidate)
                    is_always_on = trigger == "always_on"
                rule_files.setdefault(candidate.resolve(), (candidate, is_always_on))
            nested_rules = [path for path in rule_dir.rglob("*.md") if path.parent != rule_dir]
            if nested_rules:
                self.report(
                    "rules",
                    WARN,
                    nested_rules[0],
                    f"{len(nested_rules)} rule file(s) in subfolders of {self.short_path(rule_dir)}; only files "
                    "directly in rules/ are scanned.",
                    "Move them directly into rules/, or include them from a top-level rule with @[label](path).",
                )

        if not rule_files and plugin_manifest_path(self.workspace) is None:
            self.report("rules", INFO, self.workspace, "No rule files found.", "Add a short AGENTS.md with your invariants.")
        always_on_total_bytes = 0
        for resolved_path, (shown_path, is_always_on) in rule_files.items():
            expanded_bytes = expanded_rule_size(resolved_path, self.repo_root)
            if is_always_on:
                always_on_total_bytes += expanded_bytes
            warn_threshold = int(RULE_FILE_CAP_BYTES * RULE_FILE_WARN_RATIO)
            if expanded_bytes > RULE_FILE_CAP_BYTES:
                self.report(
                    "rules",
                    ERROR,
                    shown_path,
                    f"{expanded_bytes:,} bytes after includes; Antigravity drops the last "
                    f"{expanded_bytes - RULE_FILE_CAP_BYTES:,} bytes every turn.",
                    "Move long references into skills or glob-triggered rules; keep invariants at the top.",
                )
            elif expanded_bytes > warn_threshold:
                self.report(
                    "rules",
                    WARN,
                    shown_path,
                    f"{expanded_bytes:,} of {RULE_FILE_CAP_BYTES:,} bytes used.",
                    "Trim before it reaches the cap; truncation is silent.",
                )
            if resolved_path.name in STANDALONE_RULE_NAMES and read_text(resolved_path).startswith("---"):
                self.report(
                    "rules",
                    WARN,
                    shown_path,
                    "Standalone GEMINI.md/AGENTS.md does not support frontmatter; it is read as plain text.",
                    "Remove the frontmatter, or move the rule to .agents/rules/ to use a trigger.",
                )
        if always_on_total_bytes > AGGREGATE_RULE_BUDGET_BYTES:
            self.report(
                "rules",
                WARN,
                self.workspace,
                f"Always-on rules total {always_on_total_bytes:,} bytes (~{always_on_total_bytes // BYTES_PER_TOKEN_ESTIMATE:,} "
                f"tokens), over the {AGGREGATE_RULE_BUDGET_TOKENS:,}-token aggregate budget; excess rules are demoted "
                "from inline text to file path pointers.",
                "Convert language- or task-specific rules to trigger: glob or trigger: model_decision.",
            )

    def check_rule_trigger(self, rule_path: Path) -> str | None:
        frontmatter = parse_frontmatter(read_text(rule_path))
        trigger = frontmatter.values.get("trigger") if frontmatter else None
        if trigger not in RULE_TRIGGERS:
            found = f"trigger '{trigger}'" if trigger else "no trigger"
            self.report(
                "rules",
                ERROR,
                rule_path,
                f"Rule has {found}; Antigravity silently discards it.",
                f"Add frontmatter with trigger: one of {', '.join(sorted(RULE_TRIGGERS))}.",
            )
            return None
        required_key = {"model_decision": "description", "glob": "globs"}.get(trigger)
        if required_key and not (frontmatter.values.get(required_key) or frontmatter.values.get("glob")):
            self.report(
                "rules",
                ERROR,
                rule_path,
                f"trigger: {trigger} needs a '{required_key}' field.",
                f"Add '{required_key}:' to the frontmatter.",
            )
        return trigger

    def expand_manifest_dirs(
        self,
        manifest_path: Path,
        root: Path,
        check_name: str,
        seen_manifests: frozenset[Path] = frozenset(),
        parent_include_only: list[str] | None = None,
        parent_exclude: list[str] | None = None,
    ) -> set[Path]:
        resolved_manifest = manifest_path.resolve()
        if resolved_manifest in seen_manifests:
            return set()
        manifest = read_json(manifest_path)
        if manifest is None:
            self.report(check_name, ERROR, manifest_path, f"{manifest_path.name} is not valid JSON.", "Fix the JSON syntax.")
            return set()

        found: set[Path] = set()
        dot_parent = root.parent if root != self.global_root else self.global_root
        fallback_base = self.repo_root if root != self.global_root else self.global_root

        for inherited in manifest.get("inherits") or []:
            raw_inherit = inherited.get("path", "")
            inherit_path = resolve_manifest_path(raw_inherit, dot_parent, fallback_base)
            if not inherit_path.is_file():
                self.report(
                    check_name,
                    WARN,
                    manifest_path,
                    f"Inherited config '{raw_inherit}' resolves to {self.short_path(inherit_path)}, which does not exist.",
                    "Point 'inherits.path' at an existing JSON configuration file.",
                )
                continue
            found |= self.expand_manifest_dirs(
                inherit_path,
                root,
                check_name,
                seen_manifests | {resolved_manifest},
                inherited.get("include_only"),
                inherited.get("exclude"),
            )

        for entry in manifest.get("entries") or []:
            raw_path = entry.get("path", "")
            if raw_path.strip() in (".", "./") and root != self.global_root:
                self.report(
                    check_name,
                    WARN,
                    manifest_path,
                    f"Entry path '{raw_path}' resolves to the project root ({self.short_path(dot_parent)}), "
                    f"not {self.short_path(root)}.",
                    f"Use '{root.name}' to scan {root.name}/, or '{root.name}/{check_name}'.",
                )
            entry_path = resolve_manifest_path(raw_path, dot_parent, fallback_base)
            if not entry_path.is_dir():
                self.report(
                    check_name,
                    WARN,
                    manifest_path,
                    f"Entry '{raw_path}' resolves to {self.short_path(entry_path)}, which does not exist.",
                    "Relative paths resolve against the folder containing .agents/ (falling back to the repo root), not inside .agents/.",
                )
                continue
            if check_name == "skills":
                children = one_level_skill_dirs(entry_path, entry.get("include_only"), entry.get("exclude"))
                if not children and (entry_path / "SKILL.md").is_file():
                    self.report(
                        "skills",
                        WARN,
                        manifest_path,
                        f"Entry '{raw_path}' points directly at skill folder '{entry_path.name}'; "
                        "entries are scanned one level deep.",
                        f"Point 'path' at '{self.short_path(entry_path.parent)}' (with include_only: ['{entry_path.name}'] if needed).",
                    )
            else:
                children = one_level_plugin_dirs(entry_path, entry.get("include_only"), entry.get("exclude"))
                if not children and plugin_manifest_path(entry_path) is not None:
                    self.report(
                        "plugins",
                        WARN,
                        manifest_path,
                        f"Entry '{raw_path}' points directly at plugin folder '{entry_path.name}'; "
                        "entries are scanned one level deep.",
                        f"Point 'path' at '{self.short_path(entry_path.parent)}' (with include_only: ['{entry_path.name}'] if needed).",
                    )
            if parent_include_only:
                children = {
                    child for child in children if any(pattern_matches(pat, child.name) for pat in parent_include_only)
                }
            if parent_exclude:
                children = {
                    child for child in children if not any(pattern_matches(pat, child.name) for pat in parent_exclude)
                }
            found |= children
        return found

    def discoverable_skill_dirs(self) -> tuple[set[Path], set[Path]]:
        skill_dirs: set[Path] = set()
        manifest_dirs: set[Path] = set()
        roots = self.customization_roots()
        scan_dirs = [root / "skills" for root in roots] + [plugin.directory / "skills" for plugin in self.plugins()]
        scan_dirs += [self.gemini_home / "antigravity-cli" / "skills", self.gemini_home / "antigravity" / "skills"]
        cli_plugins = self.gemini_home / "antigravity-cli" / "plugins"
        if cli_plugins.is_dir():
            scan_dirs += [plugin_dir / "skills" for plugin_dir in cli_plugins.iterdir() if plugin_dir.is_dir()]
        for scan_dir in scan_dirs:
            skill_dirs |= one_level_skill_dirs(scan_dir, include_only=None, exclude=None)
        for root in roots:
            manifest_path = root / "skills.json"
            if manifest_path.is_file():
                manifest_dirs |= self.expand_manifest_dirs(manifest_path, root, "skills")
        skill_dirs |= manifest_dirs
        return {path.resolve() for path in skill_dirs}, manifest_dirs

    def check_skills(self) -> None:
        discoverable, manifest_dirs = self.discoverable_skill_dirs()
        skill_files: dict[Path, Path] = {}
        disabled_plugin_roots = {plugin.directory.resolve() for plugin in self.all_plugins() if not plugin.enabled}
        scan_roots = list(self.customization_roots())
        for plugin in self.plugins():
            if (plugin.directory / "skills").is_dir():
                scan_roots.append(plugin.directory / "skills")
        for root in scan_roots:
            for skill_file in walk_files(root, "SKILL.md"):
                resolved = skill_file.resolve()
                if any(plugin_root in resolved.parents for plugin_root in disabled_plugin_roots):
                    continue
                skill_files.setdefault(resolved, skill_file)
        for disc_dir in manifest_dirs:
            candidate = disc_dir / "SKILL.md"
            if candidate.is_file():
                resolved = candidate.resolve()
                if not any(plugin_root in resolved.parents for plugin_root in disabled_plugin_roots):
                    skill_files.setdefault(resolved, candidate)

        names_seen: dict[str, Path] = {}
        for resolved_path, shown_path in sorted(skill_files.items()):
            text = read_text(resolved_path)
            frontmatter = parse_frontmatter(text)
            if frontmatter is None:
                self.report(
                    "skills",
                    ERROR,
                    shown_path,
                    "No YAML frontmatter; the skill is skipped.",
                    "Start the file with ---, name:, description:, ---.",
                )
                continue
            raw_name = frontmatter.values.get("name")
            if not raw_name:
                self.report(
                    "skills",
                    ERROR,
                    shown_path,
                    "Frontmatter has no 'name'; SKILL.md requires both 'name' and 'description'.",
                    f"Add 'name: {resolved_path.parent.name}' to the frontmatter.",
                )
            elif not SKILL_NAME_PATTERN.match(raw_name):
                self.report(
                    "skills",
                    WARN,
                    shown_path,
                    f"Skill name '{raw_name}' is not lowercase kebab-case.",
                    "Use lowercase letters, digits, and hyphens only (e.g. 'my-skill').",
                )
            if not frontmatter.values.get("description"):
                self.report(
                    "skills",
                    ERROR,
                    shown_path,
                    "Frontmatter has no 'description'; the agent cannot tell when to use the skill.",
                    "Add 'description:' saying what the skill does and when to use it.",
                )
            for key, line_number in frontmatter.unquoted_colon_lines.items():
                self.report(
                    "skills",
                    ERROR,
                    f"{self.short_path(shown_path)}:{line_number}",
                    f"'{key}:' is a plain one-line value containing ': ', which strict YAML rejects; "
                    "the skill is dropped without a warning.",
                    f"Use a block scalar: '{key}: >-' with the text indented on the next line.",
                )
            if resolved_path.parent not in discoverable:
                self.report(
                    "skills",
                    WARN,
                    shown_path,
                    "On disk but outside every discovery path; Antigravity never loads it.",
                    "Move it to skills/<name>/SKILL.md, or add its parent folder to .agents/skills.json.",
                )
            skill_name = raw_name or resolved_path.parent.name
            if resolved_path.parent in discoverable:
                if skill_name in names_seen and names_seen[skill_name] != resolved_path:
                    self.report(
                        "skills",
                        WARN,
                        shown_path,
                        f"Skill name '{skill_name}' also used by {self.short_path(names_seen[skill_name])}; "
                        "the higher-priority one silently wins.",
                        "Rename one of them.",
                    )
                names_seen.setdefault(skill_name, resolved_path)

    def check_mcp(self) -> None:
        sources: list[tuple[str, Path, dict]] = []
        undocumented_sources: list[tuple[str, Path, dict]] = []
        documented_configs = [("", self.global_root / "mcp_config.json")]
        documented_configs += [("", root / "mcp_config.json") for root in self.workspace_roots()]
        for plugin in self.plugins():
            root_mcp_config = plugin.directory / "mcp_config.json"
            legacy_mcp_json = plugin.directory / "mcp.json"
            documented_configs.append((plugin.directory.name, root_mcp_config))
            if legacy_mcp_json.is_file():
                documented_configs.append((plugin.directory.name, legacy_mcp_json))
                if not root_mcp_config.is_file():
                    self.report(
                        "mcp",
                        INFO,
                        legacy_mcp_json,
                        "The docs disagree on this file name: antigravity.google/docs/mcp says plugins use mcp.json, "
                        "the built-in plugin skill uses mcp_config.json.",
                        f"If its servers do not appear, rename it to {self.short_path(root_mcp_config)}.",
                    )
        documented_resolved = {path.resolve() for _, path in documented_configs if path.is_file()}
        product_configs = [
            ("", path)
            for product in PRODUCT_DIR_NAMES
            if (path := self.gemini_home / product / "mcp_config.json").is_file()
            and path.resolve() not in documented_resolved
        ]
        unreadable_global_config = False
        for target, config_list in ((sources, documented_configs), (undocumented_sources, product_configs)):
            for plugin_dir_name, config_path in config_list:
                if not config_path.is_file():
                    continue
                if not os.access(config_path, os.R_OK):
                    unreadable_global_config = True
                    self.report(
                        "mcp",
                        INFO,
                        config_path,
                        f"{config_path.name} is blocked by the terminal sandbox; global MCP checks were skipped.",
                        "Run the audit in a normal terminal (or outside the sandbox) to inspect global MCP configs.",
                    )
                    continue
                config = read_json(config_path)
                if config is None:
                    self.report("mcp", ERROR, config_path, f"{config_path.name} is not valid JSON.", "Fix the JSON syntax.")
                    continue
                target.append((plugin_dir_name, config_path, config.get("mcpServers") or {}))
        for _, config_path, servers in undocumented_sources:
            if servers:
                self.report(
                    "mcp",
                    INFO,
                    config_path,
                    f"Defines {len(servers)} server(s) in a location the current docs do not list; "
                    "it may be legacy and drift from ~/.gemini/config/mcp_config.json.",
                    "Keep MCP servers in ~/.gemini/config/mcp_config.json, .agents/mcp_config.json or a plugin.",
                )

        known_cache_names: set[str] = set()
        for plugin in self.all_plugins():
            for candidate_name in ("mcp_config.json", "mcp.json"):
                candidate_path = plugin.directory / candidate_name
                if candidate_path.is_file() and os.access(candidate_path, os.R_OK):
                    for srv_name in ((read_json(candidate_path) or {}).get("mcpServers") or {}):
                        known_cache_names |= cache_name_candidates(plugin.directory.name, srv_name)
        for plugin_dir_name, _, servers in undocumented_sources:
            for server_name in servers:
                known_cache_names |= cache_name_candidates(plugin_dir_name, server_name)
        by_signature: dict[str, list[tuple[str, Path, str]]] = {}
        by_name: dict[str, list[tuple[str, Path, str]]] = {}
        for plugin_dir_name, config_path, servers in sources:
            for server_name, server in servers.items():
                if not isinstance(server, dict):
                    continue
                known_cache_names |= cache_name_candidates(plugin_dir_name, server_name)
                signature = server_signature(server)
                if signature:
                    by_signature.setdefault(signature, []).append((plugin_dir_name, config_path, server_name))
                by_name.setdefault(server_name, []).append((plugin_dir_name, config_path, signature))
                remote_url = str(server.get("url") or server.get("serverUrl") or "")
                if remote_url and remote_url.split("?", 1)[0].rstrip("/").endswith("/sse"):
                    self.report(
                        "mcp",
                        ERROR,
                        config_path,
                        f"Server '{server_name}' points at legacy HTTP+SSE endpoint '{remote_url}', "
                        "which Antigravity does not support.",
                        "Use the server's Streamable HTTP endpoint (usually ending in /mcp) or wrap it with mcp-remote via stdio.",
                    )
                for env_key, env_value in (server.get("env") or {}).items():
                    if looks_like_inline_secret(env_key, env_value):
                        self.report(
                            "mcp",
                            WARN,
                            config_path,
                            f"Server '{server_name}' has a literal value for env '{env_key}'.",
                            "Load secrets from the environment through a wrapper script, never inline.",
                        )

        reported_duplicate_configs: set[frozenset[Path]] = set()
        for signature, owners in by_signature.items():
            config_paths = frozenset(owner_path.resolve() for _, owner_path, _ in owners)
            if len(config_paths) > 1:
                reported_duplicate_configs.add(config_paths)
                described = ", ".join(f"'{name}' in {self.short_path(path)}" for _, path, name in owners)
                self.report(
                    "mcp",
                    ERROR,
                    owners[0][1],
                    f"The same server is started more than once ({described}); every tool is injected twice.",
                    "Keep one copy (the plugin's, if a plugin ships it) and remove the others.",
                )

        for server_name, owners in by_name.items():
            config_paths = frozenset(owner_path.resolve() for _, owner_path, _ in owners)
            if len(config_paths) > 1 and config_paths not in reported_duplicate_configs:
                described = ", ".join(self.short_path(path) for _, path, _ in owners)
                self.report(
                    "mcp",
                    WARN,
                    owners[0][1],
                    f"MCP server name '{server_name}' is defined in multiple configs ({described}); "
                    "server names share one global namespace and collide.",
                    "Prefix plugin server names with the plugin name, or remove the duplicate entry.",
                )

        if unreadable_global_config:
            return
        for product in PRODUCT_DIR_NAMES:
            cache_root = self.gemini_home / product / "mcp"
            if not cache_root.is_dir():
                continue
            for cache_dir in sorted(path for path in cache_root.iterdir() if path.is_dir()):
                if cache_dir.name not in known_cache_names:
                    self.report(
                        "mcp",
                        WARN,
                        cache_dir,
                        "Cached MCP server not found in any config file this audit reads; a removed server "
                        "can stay listed in the app and IDE until its cache is gone.",
                        "If it is not installed in the app's MCP settings either, delete this cache folder.",
                    )

    def check_hooks(self) -> None:
        hook_files = [root / "hooks.json" for root in self.customization_roots()]
        for plugin in self.plugins():
            root_hooks = plugin.directory / "hooks.json"
            nested_hooks = plugin.directory / "hooks" / "hooks.json"
            hook_files.append(root_hooks)
            if nested_hooks.is_file() and not root_hooks.is_file():
                self.report(
                    "hooks",
                    ERROR,
                    nested_hooks,
                    "Native plugins only load hooks.json from the plugin root; these hooks never run.",
                    f"Move it to {self.short_path(root_hooks)}.",
                )

        has_pre_tool_use = False
        seen_hook_files: set[Path] = set()
        for hooks_path in hook_files:
            if not hooks_path.is_file():
                continue
            resolved_hooks = hooks_path.resolve()
            if resolved_hooks in seen_hook_files:
                continue
            seen_hook_files.add(resolved_hooks)
            config = read_json(hooks_path)
            if not isinstance(config, dict):
                self.report("hooks", ERROR, hooks_path, "hooks.json is not a JSON object.", "Fix the JSON syntax.")
                continue
            for hook_name, spec in config.items():
                if not isinstance(spec, dict):
                    self.report("hooks", ERROR, hooks_path, f"Hook '{hook_name}' is not an object.", "See the hooks.json format.")
                    continue
                if spec.get("enabled") is False:
                    continue
                for key in spec:
                    if key not in HOOK_SPEC_KEYS:
                        self.report(
                            "hooks",
                            ERROR,
                            hooks_path,
                            f"Hook '{hook_name}' uses unknown event '{key}'; it never fires.",
                            f"Use one of: {', '.join(sorted(FLAT_HOOK_EVENTS | GROUPED_HOOK_EVENTS))}.",
                        )
                for event in GROUPED_HOOK_EVENTS & spec.keys():
                    for group in spec[event] or []:
                        if not isinstance(group, dict) or "hooks" not in group:
                            self.report(
                                "hooks",
                                ERROR,
                                hooks_path,
                                f"'{hook_name}.{event}' needs a matcher group with a 'hooks' list.",
                                'Wrap handlers as {"matcher": "run_command", "hooks": [...]}.',
                            )
                            continue
                        matcher = group.get("matcher")
                        if isinstance(matcher, str) and matcher not in ("", "*"):
                            if matcher.startswith("CORTEX_STEP_TYPE_"):
                                self.report(
                                    "hooks",
                                    WARN,
                                    hooks_path,
                                    f"'{hook_name}.{event}' matcher '{matcher}' includes the raw 'CORTEX_STEP_TYPE_' prefix; "
                                    "Antigravity matches lowercase tool names with that prefix stripped.",
                                    f"Use '{matcher.removeprefix('CORTEX_STEP_TYPE_').lower()}'.",
                                )
                            try:
                                re.compile(matcher)
                            except re.error as exc:
                                self.report(
                                    "hooks",
                                    ERROR,
                                    hooks_path,
                                    f"'{hook_name}.{event}' matcher '{matcher}' is not a valid regex ({exc}).",
                                    "Use a valid regular expression (or '*' to match all tools).",
                                )
                        handlers = group.get("hooks") or []
                        if event == "PreToolUse" and handlers:
                            has_pre_tool_use = True
                        for handler in handlers:
                            self.check_hook_command(hooks_path, hook_name, event, handler)
                for event in FLAT_HOOK_EVENTS & spec.keys():
                    for handler in spec[event] or []:
                        if not isinstance(handler, dict) or "hooks" in handler:
                            self.report(
                                "hooks",
                                ERROR,
                                hooks_path,
                                f"'{hook_name}.{event}' takes handlers directly, not a matcher group.",
                                "List the handler objects directly under the event.",
                            )
                            continue
                        self.check_hook_command(hooks_path, hook_name, event, handler)
        if not has_pre_tool_use:
            self.report(
                "hooks",
                INFO,
                self.workspace,
                "No PreToolUse hook; nothing can deny a tool call before it runs.",
                "Add one guard for the action you would least like to undo (see templates/).",
            )

    def check_hook_command(self, hooks_path: Path, hook_name: str, event: str, handler: dict) -> None:
        handler_type = handler.get("type", "command")
        if handler_type != "command":
            self.report(
                "hooks",
                ERROR,
                hooks_path,
                f"'{hook_name}.{event}' uses unsupported handler type '{handler_type}'; only 'command' is supported.",
                "Set 'type' to 'command' and invoke a shell command or script.",
            )
        command = handler.get("command")
        if not command:
            self.report("hooks", ERROR, hooks_path, f"'{hook_name}.{event}' has a handler without 'command'.", "Add a command.")
            return
        for token in extract_script_candidates(command):
            script_path = Path(os.path.expanduser(token))
            if not script_path.is_absolute():
                script_path = hooks_path.parent / script_path
            if not script_path.exists():
                self.report(
                    "hooks",
                    ERROR,
                    hooks_path,
                    f"'{hook_name}.{event}' runs {token}, which does not exist (relative paths resolve "
                    "from the folder holding hooks.json).",
                    "Fix the path.",
                )

    def check_agents(self) -> None:
        agent_dirs = [root / "agents" for root in self.customization_roots()]
        agent_dirs += [plugin.directory / "agents" for plugin in self.plugins()]
        seen_agents: set[Path] = set()
        for agents_dir in agent_dirs:
            if not agents_dir.is_dir():
                continue
            agent_files = sorted(agents_dir.glob("*.md")) + sorted(agents_dir.glob("*/agent.md"))
            for agent_file in agent_files:
                resolved_agent = agent_file.resolve()
                if resolved_agent in seen_agents:
                    continue
                seen_agents.add(resolved_agent)
                frontmatter = parse_frontmatter(read_text(agent_file))
                if frontmatter is None:
                    self.report("agents", ERROR, agent_file, "No YAML frontmatter.", "Add name, description and tools.")
                    continue
                for key, line_number in frontmatter.unquoted_colon_lines.items():
                    self.report(
                        "agents",
                        ERROR,
                        f"{self.short_path(agent_file)}:{line_number}",
                        f"'{key}:' is a plain one-line value containing ': '.",
                        f"Use '{key}: >-' with the text on the next line.",
                    )
                for key in ("name", "description"):
                    if not frontmatter.values.get(key):
                        self.report("agents", ERROR, agent_file, f"Frontmatter has no '{key}'.", f"Add '{key}:'.")
                model = frontmatter.values.get("model")
                if model and model not in AGENT_MODELS:
                    self.report(
                        "agents",
                        ERROR,
                        agent_file,
                        f"model: {model} is not a documented value.",
                        f"Use one of: {', '.join(sorted(AGENT_MODELS))}.",
                    )
                tools = frontmatter.lists.get("tools")
                unknown_tools = sorted(set(tools or []) - KNOWN_TOOL_NAMES)
                if unknown_tools:
                    self.report(
                        "agents",
                        WARN,
                        agent_file,
                        f"Tool names this audit does not recognise: {', '.join(unknown_tools)}. "
                        "A misspelled tool name can hang the subagent.",
                        "Check each name against the tools your Antigravity version lists.",
                    )
                identity = f"{frontmatter.values.get('name', '')} {frontmatter.values.get('description', '')}"
                if not REVIEWER_PATTERN.search(identity):
                    continue
                if tools is None:
                    self.report(
                        "agents",
                        WARN,
                        agent_file,
                        "Reviewer agent without a tools allowlist; check what it inherits.",
                        "List read-only tools only, e.g. view_file, grep_search, read_url_content, send_message.",
                    )
                    continue
                write_tools = sorted(WRITE_TOOL_NAMES & set(tools))
                if write_tools:
                    self.report(
                        "agents",
                        WARN,
                        agent_file,
                        f"Reviewer agent can change files: {', '.join(write_tools)}.",
                        "Remove write tools so it reports instead of editing; let the main agent apply fixes.",
                    )


@dataclass
class Frontmatter:
    values: dict[str, str]
    lists: dict[str, list[str]]
    unquoted_colon_lines: dict[str, int]


def parse_frontmatter(text: str) -> Frontmatter | None:
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return None
    try:
        end_index = next(index for index in range(1, len(lines)) if lines[index].strip() == "---")
    except StopIteration:
        return None
    values: dict[str, str] = {}
    lists: dict[str, list[str]] = {}
    unquoted_colon_lines: dict[str, int] = {}
    current_key: str | None = None
    block_scalar_key: str | None = None
    block_lines: list[str] = []

    def flush_block() -> None:
        nonlocal block_scalar_key, block_lines
        if block_scalar_key is not None:
            values[block_scalar_key] = " ".join(part for part in block_lines if part).strip()
            block_scalar_key = None
            block_lines = []

    for index in range(1, end_index):
        line = lines[index]
        top_level = re.match(r"^([A-Za-z_][\w-]*):(.*)$", line)
        if top_level:
            flush_block()
            current_key = top_level.group(1)
            raw_value = top_level.group(2).strip()
            if raw_value in (">", ">-", "|", "|-"):
                block_scalar_key = current_key
                values[current_key] = ""
                continue
            if raw_value.startswith("[") and raw_value.endswith("]"):
                inner = raw_value[1:-1].strip()
                items = [item.strip().strip("\"'") for item in inner.split(",") if item.strip()] if inner else []
                lists[current_key] = items
                values[current_key] = raw_value
                continue
            if raw_value and raw_value[0] not in "\"'>|[{" and ": " in raw_value:
                unquoted_colon_lines[current_key] = index + 1
            values[current_key] = raw_value.strip("\"'")
            continue
        if block_scalar_key is not None and (line.startswith((" ", "\t")) or not line.strip()):
            block_lines.append(line.strip())
            continue
        list_item = re.match(r"^\s+-\s+(.+)$", line)
        if list_item and current_key:
            lists.setdefault(current_key, []).append(list_item.group(1).strip().strip("\"'"))
            continue
    flush_block()
    return Frontmatter(values, lists, unquoted_colon_lines)


def extract_script_candidates(command: str) -> list[str]:
    try:
        tokens = shlex.split(command)
    except ValueError:
        return []
    if len(tokens) >= 3 and os.path.basename(tokens[0]) in ("sh", "bash", "zsh") and tokens[1] in ("-c", "-lc"):
        return extract_script_candidates(tokens[2])
    candidates: list[str] = []
    for index, token in enumerate(tokens):
        if token.startswith("-"):
            if "=" in token:
                _, value = token.split("=", 1)
                if SCRIPT_TOKEN_PATTERN.search(value):
                    candidates.append(value)
            continue
        if any(char.isspace() for char in token):
            continue
        if SCRIPT_TOKEN_PATTERN.search(token) or (index == 0 and token.startswith(("./", "../"))):
            candidates.append(token)
    return candidates


def one_level_skill_dirs(scan_dir: Path, include_only, exclude) -> set[Path]:
    if not scan_dir.is_dir():
        return set()
    found = set()
    for child in scan_dir.iterdir():
        if not (child / "SKILL.md").is_file():
            continue
        if include_only and not any(pattern_matches(pattern, child.name) for pattern in include_only):
            continue
        if exclude and any(pattern_matches(pattern, child.name) for pattern in exclude):
            continue
        found.add(child)
    return found


def plugin_manifest_path(directory: Path) -> Path | None:
    return first_existing(
        directory / "plugin.json",
        directory / ".antigravity-plugin" / "plugin.json",
        directory / ".gemini-plugin" / "plugin.json",
    )


def one_level_plugin_dirs(scan_dir: Path, include_only, exclude) -> set[Path]:
    if not scan_dir.is_dir():
        return set()
    found = set()
    for child in scan_dir.iterdir():
        if not child.is_dir():
            continue
        if plugin_manifest_path(child) is None:
            continue
        if include_only and not any(pattern_matches(pattern, child.name) for pattern in include_only):
            continue
        if exclude and any(pattern_matches(pattern, child.name) for pattern in exclude):
            continue
        found.add(child)
    return found


def pattern_matches(pattern: str, name: str) -> bool:
    try:
        if re.fullmatch(pattern, name):
            return True
    except re.error:
        pass
    return fnmatch.fnmatch(name, pattern)


def resolve_manifest_path(raw_path: str, dot_parent: Path, fallback_base: Path) -> Path:
    if raw_path.startswith("~/"):
        return Path(raw_path).expanduser()
    if raw_path.startswith("/"):
        return Path(raw_path)
    primary = (dot_parent / raw_path).resolve()
    if primary.exists() or dot_parent == fallback_base:
        return primary
    fallback = (fallback_base / raw_path).resolve()
    return fallback if fallback.exists() else primary


def expanded_rule_size(rule_path: Path, repo_root: Path | None = None, seen: frozenset[Path] = frozenset()) -> int:
    resolved = rule_path.resolve()
    if resolved in seen:
        return 0
    try:
        raw_bytes = resolved.read_bytes()
    except OSError:
        return 0
    total = len(raw_bytes)
    text = raw_bytes.decode("utf-8", errors="replace")
    for match in INCLUDE_PATTERN.finditer(text):
        include = match.group(1).strip()
        if include.startswith("~/"):
            candidates = [Path(include).expanduser()]
        elif include.startswith("/"):
            candidates = [Path(include), resolved.parent / include.lstrip("/")]
            if repo_root is not None:
                candidates.append(repo_root / include.lstrip("/"))
        else:
            candidates = [resolved.parent / include]
            if repo_root is not None:
                candidates.append(repo_root / include)
        include_path = first_existing(*candidates)
        if include_path is not None:
            total -= len(match.group(0).encode("utf-8"))
            total += expanded_rule_size(include_path, repo_root, seen | {resolved})
    return total


def server_signature(server: dict) -> str:
    if server.get("url") or server.get("serverUrl"):
        return "url:" + str(server.get("url") or server.get("serverUrl"))
    if server.get("command"):
        return "cmd:" + " ".join([str(server["command"]), *map(str, server.get("args") or [])])
    return ""


def cache_name_candidates(plugin_dir_name: str, server_name: str) -> set[str]:
    names = {server_name, server_name.replace("-", "_")}
    if plugin_dir_name:
        for plugin_variant in {plugin_dir_name, plugin_dir_name.replace("-", "_"), plugin_dir_name.split("-")[0]}:
            names |= {f"{plugin_variant}_{server_name}", f"{plugin_variant}_{server_name.replace('-', '_')}"}
        names.add(plugin_dir_name.replace("-plugin", "").replace("-", "_"))
    return names


def looks_like_inline_secret(env_key: str, env_value) -> bool:
    if not isinstance(env_value, str) or not SECRET_KEY_PATTERN.search(env_key):
        return False
    value = env_value.strip()
    return len(value) >= MIN_SECRET_LENGTH and not value.startswith("$")


def walk_files(root: Path, file_name: str):
    for directory, child_dirs, files in os.walk(root, followlinks=True):
        child_dirs[:] = [name for name in child_dirs if name not in SKIPPED_DIR_NAMES]
        if file_name in files:
            yield Path(directory) / file_name


def find_repo_root(start: Path) -> Path:
    has_own_dot_dir = any((start / dot_name).is_dir() for dot_name in DOT_DIR_NAMES)
    if (start / ".git").exists() or plugin_manifest_path(start) is not None:
        return start
    for directory in start.parents:
        if (directory / ".git").exists():
            if has_own_dot_dir and plugin_manifest_path(directory) is not None:
                return start
            return directory
    return start


def first_existing(*paths: Path) -> Path | None:
    return next((path for path in paths if path.is_file()), None)


def read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def read_jsonc(path: Path):
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return None
    without_comments = JSONC_TOKEN_PATTERN.sub(
        lambda match: match.group(0) if match.group(0).startswith('"') else "",
        raw,
    )
    cleaned = TRAILING_COMMA_PATTERN.sub(r"\1", without_comments)
    try:
        return json.loads(cleaned)
    except ValueError:
        return None


def read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def display_path(path: Path | str) -> str:
    text = str(path)
    home = str(Path.home())
    return "~" + text[len(home) :] if text.startswith(home) else text


def print_report(findings: list[Finding], workspace: Path) -> None:
    print(f"Antigravity setup audit: {display_path(workspace)}\n")
    if not findings:
        print("No findings.")
        return
    current_check = None
    for finding in findings:
        if finding.check != current_check:
            current_check = finding.check
            print(f"[{current_check}]")
        print(f"  {finding.severity:<5} {finding.path}")
        print(f"        {finding.message}")
        print(f"        fix: {finding.fix}")
    counts = {severity: sum(1 for finding in findings if finding.severity == severity) for severity in SEVERITY_ORDER}
    print(f"\nErrors: {counts[ERROR]}, warnings: {counts[WARN]}, notes: {counts[INFO]}. Nothing was changed.")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Read-only audit of a Google Antigravity setup.")
    parser.add_argument("--workspace", default=".", help="Project folder to audit (default: current folder).")
    parser.add_argument("--gemini-home", default="~/.gemini", help="Antigravity home folder (default: ~/.gemini).")
    parser.add_argument("--json", action="store_true", help="Print findings as JSON.")
    arguments = parser.parse_args(argv)

    workspace = Path(arguments.workspace).expanduser()
    if not workspace.is_dir():
        parser.error(f"workspace not found: {workspace}")
    findings = Audit(workspace, Path(arguments.gemini_home)).run()
    if arguments.json:
        print(json.dumps([asdict(finding) for finding in findings], indent=2))
    else:
        print_report(findings, workspace.resolve())
    return 1 if any(finding.severity == ERROR for finding in findings) else 0


if __name__ == "__main__":
    sys.exit(main())
