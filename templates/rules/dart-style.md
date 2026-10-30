---
trigger: glob
globs: "**/*.dart, **/pubspec.yaml"
---

# Dart and Flutter style

Loaded only when the agent touches Dart files or pubspec.yaml, so it costs nothing in other tasks.

- Run `dart format` and `dart analyze` before reporting a change as done.
- Prefer `const` constructors and small widgets over long `build` methods.
- Do not add a new state management package; use the one in pubspec.yaml.
