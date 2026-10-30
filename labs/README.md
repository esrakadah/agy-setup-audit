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
