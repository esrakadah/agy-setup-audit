---
name: agy-setup-audit
description: >-
  Audit this machine's Antigravity setup for silent failures: rule files over the
  24,000-byte cap, rules with a missing trigger, skills that never load, MCP servers
  configured twice or cached after removal, hooks that cannot run, and reviewer agents
  that can write files. Use when the user asks to audit or debug their Antigravity
  setup, or asks why a rule, skill, hook, agent or MCP server is not working.
---

# Antigravity setup audit

Read-only. Do not change any file until the user approves a specific fix.

## Steps

1. Run the audit from the workspace the user is working in:
   `python3 scripts/agy_audit.py --workspace <workspace path>`
   The script path is relative to this skill folder: [agy_audit.py](./scripts/agy_audit.py).
   Add `--json` if you need to process the findings.
2. Group the findings by severity. ERROR means something is ignored or broken right now;
   WARN means it will break or is likely unintended; INFO is a suggestion.
3. For each ERROR, explain in one sentence what the user loses (for example "this rule is
   never loaded") and propose the exact fix from the finding's `fix` line.
4. Ask before applying any fix. Never delete a cache folder or edit `mcp_config.json`
   without a yes for that specific item.
5. After fixes are applied, run the audit again and show that the finding is gone.

## Limits to mention

- The checks for YAML colons, folder depth and `skills.json` paths come from observed
  behaviour, not from the docs.
- The known tool-name list is not complete; an unknown name is a warning to check, not
  proof of a typo.
- Servers installed through the app's MCP settings UI are not in any config file, so their
  caches can show up as possibly stale.
