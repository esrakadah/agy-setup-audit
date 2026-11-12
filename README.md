# agy-setup-audit

Read-only setup auditor, `PreToolUse` branch guard, read-only critic agent, and copy-paste templates for Google Antigravity.

Companion repository to the article *I Audited My Own Antigravity Setup: 5 Silent Failures and How I Fixed Them* (not published yet), built on Mete Atamel's [Where does Antigravity look for configurations?](https://atamel.dev/posts/2026/08-21_where_agy_configuration_summary/) series and the built-in Antigravity customization reference.

Zero third-party dependencies (Python 3.9+ standard library only). The audit script never writes, moves, or deletes any file.

## Quick start

### 1. Run the audit script directly

```bash
git clone https://github.com/esrakadah/agy-setup-audit.git
python3 agy-setup-audit/skills/agy-setup-audit/scripts/agy_audit.py --workspace /path/to/your/project
```

Pass `--json` for machine-readable output, or `--gemini-home /path/to/.gemini` to inspect a non-default home directory. Exit code is `1` when at least one `ERROR` is found and `0` otherwise, so you can run it in CI against a repo's `.agents/` folder.

### 2. Try it on the broken lab workspace

`labs/broken-workspace` has one planted failure per check so you can see the output before touching your own setup:

```bash
python3 skills/agy-setup-audit/scripts/agy_audit.py \
  --workspace labs/broken-workspace \
  --gemini-home labs/empty-gemini-home
```

Expected output (`7` errors, `6` warnings, `1` note):

```text
Antigravity setup audit: <repo>/labs/broken-workspace

[agents]
  ERROR .agents/plugins/team-kit/agents/code-reviewer.md
        model: gemini-pro is not a documented value.
        fix: Use one of: flash, flash_lite, inherit, pro.
  WARN  .agents/plugins/team-kit/agents/code-reviewer.md
        Tool names this audit does not recognise: veiw_file. A misspelled tool name can hang the subagent.
        fix: Check each name against the tools your Antigravity version lists.
  WARN  .agents/plugins/team-kit/agents/code-reviewer.md
        Reviewer agent can change files: write_to_file.
        fix: Remove write tools so it reports instead of editing; let the main agent apply fixes.
[hooks]
  ERROR .agents/hooks.json
        Hook 'format-check' uses unknown event 'BeforeTool'; it never fires.
        fix: Use one of: PostInvocation, PostToolUse, PreInvocation, PreToolUse, SessionStart, Stop.
  ERROR .agents/hooks.json
        'test-reminder.Stop' runs scripts/remind_tests.py, which does not exist (relative paths resolve from the folder holding hooks.json).
        fix: Fix the path.
  ERROR .agents/plugins/team-kit/hooks/hooks.json
        Native plugins only load hooks.json from the plugin root; these hooks never run.
        fix: Move it to .agents/plugins/team-kit/hooks.json.
  INFO  .
        No PreToolUse hook; nothing can deny a tool call before it runs.
        fix: Add one guard for the action you would least like to undo (see templates/).
[mcp]
  ERROR .agents/mcp_config.json
        The same server is started more than once ('dart' in .agents/mcp_config.json, 'dart-mcp-server' in .agents/plugins/team-kit/mcp_config.json); every tool is injected twice.
        fix: Keep one copy (the plugin's, if a plugin ships it) and remove the others.
  WARN  .agents/mcp_config.json
        Server 'issue-tracker' has a literal value for env 'TRACKER_API_TOKEN'.
        fix: Load secrets from the environment through a wrapper script, never inline.
[rules]
  ERROR .agents/rules/always-on.md
        Rule has trigger 'alwaysOn'; Antigravity silently discards it.
        fix: Add frontmatter with trigger: one of always_on, glob, manual, model_decision.
  WARN  .agents/rules/team/naming.md
        1 rule file(s) in subfolders of .agents/rules; only files directly in rules/ are scanned.
        fix: Move them directly into rules/, or include them from a top-level rule with @[label](path).
[skills]
  ERROR .agents/skills/release-notes/SKILL.md:3
        'description:' is a plain one-line value containing ': ', which strict YAML rejects; the skill is dropped without a warning.
        fix: Use a block scalar: 'description: >-' with the text indented on the next line.
  WARN  .agents/release-checklist/SKILL.md
        On disk but outside every discovery path; Antigravity never loads it.
        fix: Move it to skills/<name>/SKILL.md, or add its parent folder to .agents/skills.json.
  WARN  .agents/skills.json
        Entry path '.' resolves to the project root (.), not .agents.
        fix: Use '.agents' to scan .agents/, or '.agents/skills'.

Errors: 7, warnings: 6, notes: 1. Nothing was changed.
```

### 3. Install as an Antigravity plugin

```bash
agy plugin validate --strict /path/to/agy-setup-audit
agy plugin install /path/to/agy-setup-audit
```

Or copy/symlink the repository into `.agents/plugins/agy-setup-audit` (workspace) or `~/.gemini/config/plugins/agy-setup-audit` (global). **Restart Antigravity** after installing a new plugin directory so its skills, agent, and hook are discovered on startup.

Installing the plugin registers three customizations:

- **Skill `agy-setup-audit`** (`skills/agy-setup-audit/SKILL.md`): runs the audit script on your workspace, groups findings by severity, and asks before applying any fix.
- **Subagent `setup-critic`** (`agents/setup-critic.md`): read-only reviewer restricted to `view_file`, `list_dir`, `grep_search`, `find_by_name`, `read_url_content`, and `send_message`.
- **Hook `agy-setup-audit-protected-push`** (`hooks.json` + `scripts/guard_protected_push.py`): `PreToolUse` guard on `run_command` that denies `git push` to `main` or `master` (override with `AGY_PROTECTED_BRANCHES="main,release"`), denies `git push --all` / `--mirror`, and returns `force_ask` on force-pushes to feature branches. Fails open if the payload cannot be parsed so a hook error never locks the editor.

## What the audit checks

| Area | Check | Severity | Source |
| :--- | :--- | :--- | :--- |
| `plugins` | Duplicate plugin `name` across enabled plugins (second plugin is dropped silently) or invalid JSONC in `plugin.json` | `ERROR` | Built-in docs |
| `plugins` | Workspace `plugin.json` with URL/absolute `logo`, `suggestedPrompts` > 3, or undocumented top-level keys | `WARN` | Built-in docs |
| `rules` | Rule file over 24,000 bytes after `@[...](...)` includes (or over 80% of cap) | `ERROR` / `WARN` | Built-in docs |
| `rules` | Always-on rules exceeding the 20,000-token (~80,000-byte) aggregate rules budget (excess rules demoted to path pointers) | `WARN` | Built-in docs |
| `rules` | Rule in `rules/*.md` missing valid `trigger:` (`always_on`, `model_decision`, `glob`, `manual`) or required `description` / `globs` | `ERROR` | Built-in docs |
| `rules` | Standalone `GEMINI.md` / `AGENTS.md` with YAML frontmatter, or rules nested in `rules/<subfolder>/` | `WARN` | Built-in docs |
| `skills` | Missing YAML frontmatter, missing `description`, or invalid `skills.json` | `ERROR` | Built-in docs |
| `skills` | Unquoted `: ` on a single-line frontmatter field (dropped by strict YAML parser) | `ERROR` | Observed runtime |
| `skills` | `SKILL.md` outside discovery paths (including `inherits`), `"path": "."` trap or missing `skills.json` entry, or duplicate skill names | `WARN` | Built-in docs + observed |
| `mcp` | Same server command/URL configured in two places, or legacy HTTP+SSE endpoint ending in `/sse` | `ERROR` | Built-in docs + observed |
| `mcp` | Duplicate MCP server name across configs, literal secret in `env`, or cached server under `~/.gemini/*/mcp/` not found in any config file this audit reads | `WARN` | Built-in docs + Mete's MCP post |
| `mcp` | Plugin ships `mcp.json` without `mcp_config.json` (the web docs and the built-in plugin skill name different files) | `INFO` | Both docs, conflicting |
| `hooks` | Unknown hook event, missing matcher group on `PreToolUse` / `PostToolUse`, `hooks/hooks.json` inside a native plugin instead of plugin root, or missing script file | `ERROR` | Built-in docs + Mete's hooks post |
| `agents` | Missing `name` / `description`, undocumented `model:` value, or unquoted `: ` in frontmatter | `ERROR` | Built-in docs |
| `agents` | Reviewer / critic / auditor agent with write tools or unrecognised tool names | `WARN` | Built-in docs + observed |

## Copy-paste templates (`templates/`)

If you do not want the full plugin, copy only the pieces you need into `.agents/`:

- `templates/rules/dart-style.md`: `glob`-triggered rule (`**/*.dart, **/pubspec.yaml`) that costs zero tokens outside Dart tasks.
- `templates/rules/release-process.md`: `model_decision` rule where only the description loads up front.
- `templates/skills/release-notes/SKILL.md`: skill template using `description: >-` so colons never break YAML parsing.
- `templates/skills.json`: manifest template showing repo-root-relative paths and `exclude` filters.
- `templates/agents/code-reviewer.md`: read-only reviewer subagent with a strict 5-tool allowlist (`view_file`, `list_dir`, `grep_search`, `find_by_name`, `send_message`).
- `templates/style-gate/`: `Stop` hook (`stop_style_gate.py`, `banned_phrases.txt`, `hooks.json`) that sends a reply back once (`executionNum == 1` on `terminationReason == "NO_TOOL_CALL"`) if prose outside code blocks contains an em-dash or a banned filler word.

## Tests

```bash
python3 -m unittest discover -s tests -v
```

## License

MIT
