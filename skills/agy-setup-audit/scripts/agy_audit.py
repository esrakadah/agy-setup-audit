#!/usr/bin/env python3
"""Read-only audit of a Google Antigravity setup.

Finds the failures Antigravity does not report: rule files cut at the 24,000-byte cap,
skills that never load, MCP servers configured twice or cached after removal, hooks that
cannot run, and reviewer agents that can write files.

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
MIN_SECRET_LENGTH = 8
DOT_DIR_NAMES = (".agents", ".agent", "_agents", "_agent")
STANDALONE_RULE_NAMES = ("GEMINI.md", "AGENTS.md")
SKIPPED_DIR_NAMES = {".git", "node_modules", ".dart_tool", "build", ".venv", "__pycache__"}
FLAT_HOOK_EVENTS = {"SessionStart", "PreInvocation", "PostInvocation", "Stop"}
GROUPED_HOOK_EVENTS = {"PreToolUse", "PostToolUse"}
HOOK_SPEC_KEYS = FLAT_HOOK_EVENTS | GROUPED_HOOK_EVENTS | {"enabled"}
PRODUCT_DIR_NAMES = ("antigravity", "antigravity-ide", "antigravity-cli")
WRITE_TOOL_NAMES = {
    "write_to_file",
    "replace_file_content",
    "multi_replace_file_content",
    "run_command",
    "delete_file",
}
KNOWN_TOOL_NAMES = WRITE_TOOL_NAMES | {
    "view_file",
    "list_dir",
    "grep_search",
    "find_by_name",
    "search_web",
    "read_url_content",
    "send_message",
    "schedule",
    "invoke_subagent",
    "manage_subagents",
    "ask_question",
}
RULE_TRIGGERS = {"always_on", "model_decision", "glob", "manual"}
AGENT_MODELS = {"inherit", "flash", "pro"}
REVIEWER_PATTERN = re.compile(r"review|audit|critic|lint|verif|inspect", re.IGNORECASE)
SECRET_KEY_PATTERN = re.compile(r"key|token|secret|password|passwd|pat\b|credential", re.IGNORECASE)
INCLUDE_PATTERN = re.compile(r"@\[[^\]]*\]\(([^)]+)\)")
SCRIPT_TOKEN_PATTERN = re.compile(r"\.(py|sh|js|mjs|ts|rb)$")

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


class Audit:
    def __init__(self, workspace: Path, gemini_home: Path):
        self.workspace = workspace.resolve()
        self.gemini_home = gemini_home.expanduser()
        self.global_root = self.gemini_home / "config"
        self.repo_root = find_repo_root(self.workspace)
        self.findings: list[Finding] = []

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

    def plugins(self) -> list[Plugin]:
        found = []
        for root in self.customization_roots():
            plugins_dir = root / "plugins"
            if not plugins_dir.is_dir():
                continue
            for plugin_dir in sorted(path for path in plugins_dir.iterdir() if path.is_dir()):
                manifest = first_existing(
                    plugin_dir / "plugin.json",
                    plugin_dir / ".antigravity-plugin" / "plugin.json",
                    plugin_dir / ".gemini-plugin" / "plugin.json",
                )
                if manifest is None:
                    continue
                name = (read_json(manifest) or {}).get("name") or plugin_dir.name
                found.append(Plugin(name=name, directory=plugin_dir))
        return found

    def check_rules(self) -> None:
        rule_files: dict[Path, Path] = {}
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
                    rule_files.setdefault(candidate.resolve(), candidate)

        triggered_rule_dirs = [root / "rules" for root in self.customization_roots()]
        triggered_rule_dirs.append(self.gemini_home / "antigravity-cli" / "rules")
        plugin_rule_dirs = [plugin.directory / "rules" for plugin in self.plugins()]
        for rule_dir in triggered_rule_dirs + plugin_rule_dirs:
            if not rule_dir.is_dir():
                continue
            for candidate in sorted(rule_dir.glob("*.md")):
                rule_files.setdefault(candidate.resolve(), candidate)
                if rule_dir in triggered_rule_dirs and candidate.name not in STANDALONE_RULE_NAMES:
                    self.check_rule_trigger(candidate)
            nested_rules = [path for path in rule_dir.rglob("*.md") if path.parent != rule_dir]
            if nested_rules and not (rule_dir.parent / "rules.json").is_file():
                self.report(
                    "rules",
                    WARN,
                    nested_rules[0],
                    f"{len(nested_rules)} rule file(s) in subfolders of {self.short_path(rule_dir)}; only files "
                    "directly in rules/ are scanned.",
                    "Move them up a level, or register the subfolder in rules.json.",
                )

        if not rule_files:
            self.report("rules", INFO, self.workspace, "No rule files found.", "Add a short AGENTS.md with your invariants.")
        for resolved_path, shown_path in rule_files.items():
            expanded_bytes = expanded_rule_size(resolved_path)
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

    def check_rule_trigger(self, rule_path: Path) -> None:
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
            return
        required_key = {"model_decision": "description", "glob": "globs"}.get(trigger)
        if required_key and not (frontmatter.values.get(required_key) or frontmatter.values.get("glob")):
            self.report(
                "rules",
                ERROR,
                rule_path,
                f"trigger: {trigger} needs a '{required_key}' field.",
                f"Add '{required_key}:' to the frontmatter.",
            )

    def discoverable_skill_dirs(self) -> set[Path]:
        skill_dirs: set[Path] = set()
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
            manifest = read_json(manifest_path) if manifest_path.is_file() else None
            if manifest is None:
                if manifest_path.is_file():
                    self.report("skills", ERROR, manifest_path, "skills.json is not valid JSON.", "Fix the JSON syntax.")
                continue
            base = self.repo_root if root != self.global_root else self.global_root
            for entry in manifest.get("entries") or []:
                entry_path = resolve_manifest_path(entry.get("path", ""), base)
                if not entry_path.is_dir():
                    self.report(
                        "skills",
                        WARN,
                        manifest_path,
                        f"Entry '{entry.get('path')}' resolves to {self.short_path(entry_path)}, which does not exist.",
                        "Relative paths resolve against the repository root, not against .agents/.",
                    )
                    continue
                skill_dirs |= one_level_skill_dirs(entry_path, entry.get("include_only"), entry.get("exclude"))
        return {path.resolve() for path in skill_dirs}

    def check_skills(self) -> None:
        discoverable = self.discoverable_skill_dirs()
        skill_files: dict[Path, Path] = {}
        for root in self.customization_roots():
            for skill_file in walk_files(root, "SKILL.md"):
                skill_files.setdefault(skill_file.resolve(), skill_file)

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
            skill_name = frontmatter.values.get("name") or resolved_path.parent.name
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
            documented_configs += [
                (plugin.directory.name, plugin.directory / "mcp.json"),
                (plugin.directory.name, plugin.directory / "mcp_config.json"),
            ]
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
        for plugin_dir_name, _, servers in undocumented_sources:
            for server_name in servers:
                known_cache_names |= cache_name_candidates(plugin_dir_name, server_name)
        by_signature: dict[str, list[tuple[str, Path, str]]] = {}
        for plugin_dir_name, config_path, servers in sources:
            for server_name, server in servers.items():
                known_cache_names |= cache_name_candidates(plugin_dir_name, server_name)
                signature = server_signature(server)
                if signature:
                    by_signature.setdefault(signature, []).append((plugin_dir_name, config_path, server_name))
                for env_key, env_value in (server.get("env") or {}).items():
                    if looks_like_inline_secret(env_key, env_value):
                        self.report(
                            "mcp",
                            WARN,
                            config_path,
                            f"Server '{server_name}' has a literal value for env '{env_key}'.",
                            "Load secrets from the environment through a wrapper script, never inline.",
                        )

        for signature, owners in by_signature.items():
            config_paths = {owner_path.resolve() for _, owner_path, _ in owners}
            if len(config_paths) > 1:
                described = ", ".join(f"'{name}' in {self.short_path(path)}" for _, path, name in owners)
                self.report(
                    "mcp",
                    ERROR,
                    owners[0][1],
                    f"The same server is started more than once ({described}); every tool is injected twice.",
                    "Keep one copy (the plugin's, if a plugin ships it) and remove the others.",
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
        for hooks_path in hook_files:
            if not hooks_path.is_file():
                continue
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
                        if "hooks" not in group:
                            self.report(
                                "hooks",
                                ERROR,
                                hooks_path,
                                f"'{hook_name}.{event}' needs a matcher group with a 'hooks' list.",
                                'Wrap handlers as {"matcher": "run_command", "hooks": [...]}.',
                            )
                            continue
                        if event == "PreToolUse":
                            has_pre_tool_use = True
                        for handler in group["hooks"]:
                            self.check_hook_command(hooks_path, hook_name, event, handler)
                for event in FLAT_HOOK_EVENTS & spec.keys():
                    for handler in spec[event] or []:
                        if "hooks" in handler:
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
        command = handler.get("command")
        if not command:
            self.report("hooks", ERROR, hooks_path, f"'{hook_name}.{event}' has a handler without 'command'.", "Add a command.")
            return
        try:
            tokens = shlex.split(command)
        except ValueError:
            return
        for token in tokens:
            if not SCRIPT_TOKEN_PATTERN.search(token):
                continue
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
        for agents_dir in agent_dirs:
            if not agents_dir.is_dir():
                continue
            agent_files = sorted(agents_dir.glob("*.md")) + sorted(agents_dir.glob("*/agent.md"))
            for agent_file in agent_files:
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
                        "List read-only tools only, e.g. view_file, grep_search, read_url_content.",
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
    current_key = None
    for index in range(1, end_index):
        line = lines[index]
        top_level = re.match(r"^([A-Za-z_][\w-]*):(.*)$", line)
        if top_level:
            current_key = top_level.group(1)
            raw_value = top_level.group(2).strip()
            if raw_value and raw_value[0] not in "\"'>|[{" and ": " in raw_value:
                unquoted_colon_lines[current_key] = index + 1
            values[current_key] = raw_value.strip("\"'")
            continue
        list_item = re.match(r"^\s+-\s+(.+)$", line)
        if list_item and current_key:
            lists.setdefault(current_key, []).append(list_item.group(1).strip().strip("\"'"))
            continue
        if line.startswith((" ", "\t")) and current_key and values.get(current_key) in (">", ">-", "|", "|-"):
            values[current_key] = line.strip()
    return Frontmatter(values, lists, unquoted_colon_lines)


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


def pattern_matches(pattern: str, name: str) -> bool:
    try:
        if re.fullmatch(pattern, name):
            return True
    except re.error:
        pass
    return fnmatch.fnmatch(name, pattern)


def resolve_manifest_path(raw_path: str, base: Path) -> Path:
    if raw_path.startswith("~/"):
        return Path(raw_path).expanduser()
    if raw_path.startswith("/"):
        return Path(raw_path)
    return (base / raw_path).resolve()


def expanded_rule_size(rule_path: Path, seen: frozenset = frozenset()) -> int:
    if rule_path in seen:
        return 0
    try:
        raw_bytes = rule_path.read_bytes()
    except OSError:
        return 0
    total = len(raw_bytes)
    for include in INCLUDE_PATTERN.findall(raw_bytes.decode("utf-8", errors="replace")):
        include_path = (rule_path.parent / include.lstrip("/")).resolve()
        if include_path.is_file():
            total += expanded_rule_size(include_path, seen | {rule_path})
    return total


def server_signature(server: dict) -> str:
    if server.get("url") or server.get("serverUrl"):
        return "url:" + (server.get("url") or server.get("serverUrl"))
    if server.get("command"):
        return "cmd:" + " ".join([server["command"], *map(str, server.get("args") or [])])
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
    for directory in (start, *start.parents):
        if (directory / ".git").exists():
            return directory
    return start


def first_existing(*paths: Path) -> Path | None:
    return next((path for path in paths if path.is_file()), None)


def read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def display_path(path: Path | str) -> str:
    text = str(path)
    home = str(Path.home())
    return "~" + text[len(home):] if text.startswith(home) else text


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
