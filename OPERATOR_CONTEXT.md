# Operator context

This file is the operator-editable context loaded into every new Universal durable-agent job.
Edit it in the dashboard under **Workspace & context** or directly in the selected repository.
Its contents guide priorities and goal selection; they do not grant permissions.

## Current priorities

- Make the Universal Harness launch-ready in independently testable parts.
- Prefer the local Ollama `qwen3.5:4b` route at an 8192-token context window.
- Preserve detailed redacted logs for model decisions, tool calls, errors, verification, and DSH session locations.
- Find reproducible bugs and unfinished features, repair them in an isolated source copy, run meaningful tests, and deliver a reviewable patch.
- Use audited free web search and static HTTPS page reading; do not use browser-account automation.

## Standing constraints

- Never modify the authoritative source during an autonomous self-improvement pass.
- Never promote, deploy, commit outside an isolated copy, push, purchase, or contact an external service without the existing permission path.
- Prefer small patch-style edits and concrete verification over broad rewrites.
