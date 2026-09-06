# Generation record

This example is being exercised as a real coding-client integration challenge, not as a mock-model test.

## Initial checkpoint

- Actual client: Claude Code 2.1.261, native Linux executable.
- Actual model: DeepSeek V4 Pro, explicitly configured with thinking disabled.
- Transport: Claude Code → emutools Anthropic adapter → budget guard → DeepSeek Chat Completions. No native upstream tool definitions.
- The client inspected a synthetic SQLite database through read-only MCP tools, then authored `app.py`, `build.py`, and the three `web/` files through real Bash tool calls.
- The initial authoring run began with no application source. No application implementation was supplied by the reviewer.
- The conservative per-run spending reservation stopped that session before the client completed its own build/test workflow.
- The independent verifier subsequently built and ran this checkpoint: 29/31 checks passed. Unicode search and concurrent idempotent replay failed.
- Additional real-HTTP testing found `/style.css` returned 404. Repeated competing transfers violated stock conservation in 16/25 pairs.

This is a **failed initial checkpoint**, preserved for provenance and regression review. Those failures were sent back to the real coding client for a source-only continuation. This file records the initial state, not a passing release claim.

All database contents are disposable seeded fixtures. Do not deploy this example as a production inventory service.
