---
name: code-reviewer
description: >-
  Reviews pull requests and reports problems.
model: gemini-pro
tools:
  - veiw_file
  - grep_search
  - write_to_file
  - send_message
---

# Code reviewer

Review the diff and report findings.
