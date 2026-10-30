---
name: release-notes
description: >-
  Draft release notes from merged pull requests since the last tag: what changed,
  what users notice, and what they must do. Use when the user asks for release notes
  or a changelog entry.
---

# Release notes

The `>-` block scalar above keeps colons in the description safe. A one-line
`description: Draft release notes: ...` can make the whole skill disappear.

1. Find the last tag: `git describe --tags --abbrev=0`.
2. List merged changes since then: `git log <tag>..HEAD --merges --oneline`.
3. Group them as Added, Changed, Fixed. Skip internal refactors.
4. Show the draft to the user; do not commit it.
