---
name: agy-setup-audit
description: >-
  Audit this machine's Antigravity setup for silent failures: rule files over the
  24,000-byte cap or 20,000-token aggregate budget, rules with a missing trigger, skills
  that never load, MCP servers configured twice, using legacy /sse URLs, or cached after
  removal, plugins with duplicate names, hooks that cannot run, and reviewer agents that
  can write files. Use when the user asks to audit or debug their Antigravity setup, or
  asks why a rule, skill, hook, agent, plugin, or MCP server is not working. Not for
  managing plugin lifecycle (use plugin), reading general customization reference docs
  (use agy-customizations), or running a multi-lens deliverable GBU audit (use gbu-review).
---

# Antigravity Setup Audit

## 1. Safety First & Write Gate

1. **Strictly Read-Only Audit:** `agy_audit.py` never writes, moves, or deletes any file. Do not edit any file until the user approves a specific fix.
2. **Pre-Fix Git & Backup Guard:**
   - Before applying any approved fix inside a repository, run `git status --short` so existing uncommitted changes are preserved.
   - Before editing any file outside git (such as `~/.gemini/GEMINI.md` or `~/.gemini/config/mcp_config.json`), copy it to `<filename>.v1.bak` so the post-fix verification pass can diff against the baseline.
   - Never delete an MCP cache directory or edit `mcp_config.json` without an explicit yes naming that target.

## 2. Canonical References (True as of 2026-10-30, Antigravity 2.0)

Every documented check in [agy_audit.py](./scripts/agy_audit.py) is anchored to the built-in Antigravity customization reference on disk:
- `~/.gemini/antigravity/builtin/skills/agy-customizations/SKILL.md` and `docs/{rules,skills,plugins,hooks,mcp_servers,json_configs}.md`
- `~/.gemini/antigravity/builtin/skills/plugin/SKILL.md`

Checks derived from observed runtime behavior (strict YAML colon rejection, one-level folder scan depth, and `~/.gemini/<product>/mcp/` cache directories) are explicitly labeled as observed behavior.

## 3. Execution Steps & Paid-For Replacements

1. **Run the read-only audit** from the workspace the user is working in:
   ```bash
   python3 scripts/agy_audit.py --workspace <workspace_path>
   ```
   The script path is relative to this skill folder: [agy_audit.py](./scripts/agy_audit.py). Add `--json` when programmatic filtering is needed.
2. **Paid-for replacement patterns when proposing fixes:**
   - **Terminal sandbox blocks `~/.gemini/config/mcp_config.json`:** Antigravity's Seatbelt sandbox blocks reading `mcp_config.json` (`os.access(path, os.R_OK) == False`). When `agy_audit.py` prints the sandbox `INFO` note, rerun `agy_audit.py` outside the sandbox (or ask the user to run it in their terminal) before judging global MCP servers or caches.
   - **Unquoted `: ` in single-line YAML frontmatter:** Replace `description: Foo: bar` with a block scalar:
     ```yaml
     description: >-
       Foo: bar
     ```
   - **`"path": "."` in `.agents/skills.json`:** Relative paths resolve against the folder containing `.agents/` (falling back to the repository root), not inside `.agents/`. Replace `"path": "."` with:
     ```json
     {
       "entries": [
         {
           "path": ".agents",
           "exclude": ["plugins", "skills", "rules", "agents", "logs", ".git"]
         }
       ]
     }
     ```
3. **Group findings by severity (`ERROR`, `WARN`, `INFO`)** using the Output Template below and wait for the user's yes before applying any fix.

## 4. Output Template

```markdown
# Antigravity Setup Audit: <workspace_path>

- **Summary:** <N> errors, <M> warnings, <K> notes (read-only; no files changed)

## 1. Errors (Broken or Ignored Right Now)
- **`<path>` (`<check>`):** <what is lost in one sentence>
  - **Fix:** `<exact command or edit>`

## 2. Warnings (Likely Unintended or Near Cap)
- **`<path>` (`<check>`):** <impact>
  - **Fix:** `<exact command or edit>`

## 3. Notes & Next Step
- Which items should I fix? (External config files will be backed up to `.v1.bak` first.)
```

## 5. Close-Out Ritual

1. After applying approved fixes, rerun `python3 scripts/agy_audit.py --workspace <workspace_path>` and show the before/after finding counts.
2. Run `git status --short` in the workspace (and list any `.v1.bak` backups created) so the user has a clean record of every file touched.
3. If a new plugin directory was created or installed, remind the user to restart Antigravity so the new plugin is discovered.
