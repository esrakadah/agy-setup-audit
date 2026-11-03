# Broken Workspace Lab (`labs/broken-workspace`)

A self-contained workspace with 14 planted findings (`7` errors, `6` warnings, `1` note) so you can run `agy_audit.py` and inspect every finding class before touching your own setup.

## Run the lab

From the repository root:

```bash
python3 skills/agy-setup-audit/scripts/agy_audit.py \
  --workspace labs/broken-workspace \
  --gemini-home labs/empty-gemini-home
```

## Planted findings

| Area | File | Severity | What is planted |
| :--- | :--- | :--- | :--- |
| `agents` | `.agents/plugins/team-kit/agents/code-reviewer.md` | `ERROR` | `model: gemini-pro` (undocumented model value; valid values are `inherit`, `flash`, `flash_lite`, `pro`) |
| `agents` | `.agents/plugins/team-kit/agents/code-reviewer.md` | `WARN` | `veiw_file` typo in `tools:` allowlist |
| `agents` | `.agents/plugins/team-kit/agents/code-reviewer.md` | `WARN` | Reviewer agent includes `write_to_file` in `tools:` |
| `hooks` | `.agents/hooks.json` | `ERROR` | Unknown hook event `BeforeTool` |
| `hooks` | `.agents/hooks.json` | `ERROR` | `Stop` handler points to missing `scripts/remind_tests.py` |
| `hooks` | `.agents/plugins/team-kit/hooks/hooks.json` | `ERROR` | Native plugin places `hooks.json` inside `hooks/` instead of plugin root |
| `hooks` | `.` | `INFO` | No `PreToolUse` hook configured |
| `mcp` | `.agents/mcp_config.json` & `.agents/plugins/team-kit/mcp_config.json` | `ERROR` | Duplicate `dart mcp-server` command started in two configs |
| `mcp` | `.agents/mcp_config.json` | `WARN` | Literal API token in `env.TRACKER_API_TOKEN` |
| `rules` | `.agents/rules/always-on.md` | `ERROR` | `trigger: alwaysOn` instead of `always_on` |
| `rules` | `.agents/rules/team/naming.md` | `WARN` | Rule nested in a subfolder of `.agents/rules/` |
| `skills` | `.agents/skills/release-notes/SKILL.md:3` | `ERROR` | Unquoted `: ` on a single-line `description:` |
| `skills` | `.agents/release-checklist/SKILL.md` | `WARN` | Skill folder outside every discovery path |
| `skills` | `.agents/skills.json` | `WARN` | `"path": "."` resolves to the project root, not `.agents/` |

## Exercises

Work on a copy so the lab stays broken for the next person:

```bash
cp -R labs/broken-workspace /tmp/agy-lab && cd /tmp/agy-lab && git init -q
```

Run the audit after each step (`python3 <repo>/skills/agy-setup-audit/scripts/agy_audit.py --workspace . --gemini-home <repo>/labs/empty-gemini-home`) and watch the count drop.

1. **Get a skill back.** Rewrite the `description:` in `.agents/skills/release-notes/SKILL.md` as a `>-` block scalar. Then make `release-checklist` load: move it under `.agents/skills/`, or fix `.agents/skills.json` so its entry reaches `.agents`.
2. **Rescue a rule.** Change `trigger: alwaysOn` to `always_on`, and move `rules/team/naming.md` up one level.
3. **One Dart server, not two.** Delete the `dart` entry from `.agents/mcp_config.json` and keep the plugin's. Replace the literal token with a wrapper script that reads it from the environment.
4. **Make the hooks run.** Rename `BeforeTool` to `PreToolUse` inside a `run_command` matcher group, and create both scripts the workspace hooks call under `.agents/scripts/`. Then move the plugin's `hooks/hooks.json` to the plugin root. Run the audit: now that the plugin hook is loaded, its own bug shows up (`guard.py` does not exist). Create it too. Fixing one silent failure often reveals the next.
5. **Disarm the reviewer.** In `code-reviewer.md`, fix `veiw_file`, remove `write_to_file`, and set `model: inherit`.

After step 5 the audit prints `No findings.` and exits with `0`.

6. **Break the cap on purpose.** Push `AGENTS.md` past 24,000 bytes:

   ```bash
   yes "- Prefer const constructors and small widgets." | head -n 600 >> AGENTS.md
   ```

   The audit now reports `28,272 bytes after includes; Antigravity drops the last 4,272 bytes every turn.` That is the failure that hid my most important rule.
