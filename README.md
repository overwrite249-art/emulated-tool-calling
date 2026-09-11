# emutools

[![Tests](https://github.com/overwrite249-art/emulated-tool-calling/actions/workflows/tests.yml/badge.svg)](https://github.com/overwrite249-art/emulated-tool-calling/actions/workflows/tests.yml)

**Tool calling for coding clients through a text-only, OpenAI-compatible model backend.**

emutools is a dependency-free Python 3.9+ proxy. It accepts Anthropic Messages or OpenAI
Chat Completions, renders tool schemas into the prompt, and turns the model's text back
into native `tool_use` / `tool_calls` responses. It never sends a native `tools` field upstream.

- **Claude Code:** Anthropic Messages, including streaming and token counting.
- **OpenAI-compatible clients:** Chat Completions, including streaming tool arguments and usage.
- **MCP tools:** tools registered by the client are translated like other client tools; emutools is not itself an MCP server.

## Quick start

```bash
export EMU_UPSTREAM_API_KEY=sk-...  # or DEEPSEEK_API_KEY; never commit your real key
python3 -m emutools
```

The default upstream is `https://api.deepseek.com`, routing main models to
`deepseek-v4-pro` and small/fast aliases to `deepseek-flash`. Both ids come from
`GET https://api.deepseek.com/models`; check that list if a request fails with an
unknown-model error.

### Claude Code

```bash
ANTHROPIC_BASE_URL=http://127.0.0.1:8787 ANTHROPIC_API_KEY=dummy claude
```

The client-side key is not an upstream credential. The proxy reads the real key from
its own environment. For the `claude --bare` mode used by the smoke test, supply
`ANTHROPIC_API_KEY`, not only an auth token.

### OpenCode

Use an explicit OpenAI-compatible provider in `opencode.json` rather than relying on
`OPENAI_BASE_URL` alone:

```json
{
  "$schema": "https://opencode.ai/config.json",
  "model": "emutools/deepseek-v4-pro",
  "provider": {
    "emutools": {
      "npm": "@ai-sdk/openai-compatible",
      "name": "emutools",
      "options": {
        "baseURL": "http://127.0.0.1:8787/v1",
        "apiKey": "dummy"
      },
      "models": {
        "deepseek-v4-pro": {"name": "DeepSeek V4 Pro"}
      }
    }
  }
}
```

```bash
opencode run --model emutools/deepseek-v4-pro "Describe this project"
```

For other clients that support these environment variables:

```bash
export OPENAI_BASE_URL=http://127.0.0.1:8787/v1
export OPENAI_API_KEY=dummy
```

**Keep the proxy on loopback.** Client credentials are not checked. Do not expose it to
an untrusted network without an authenticated reverse proxy and appropriate access controls.
Keep your coding client's tool permissions enabled.

## What the hardening fixes

These were all found by driving a real client against a real text-only model, not by
reading the code. Each one silently ended the client's task while looking like success.

- **Vendor channel markers never reach the user.** Real DeepSeek output frames tool calls
  with `<｜｜DSML｜｜ calls>` and `｜tool▁calls▁begin｜`; only the `<tool_calls>` spelling was
  stripped, so every assistant message carried visible garbage. Markers are now removed in
  every dialect, at any chunk boundary, without touching identical text inside arguments.
- **A rejected call no longer ends the task.** The streaming path used to report
  "[tool guard] Rejected tool call" to the user as the assistant's answer, so one guessed
  tool name finished the job. The rejection is now re-asked upstream inside the same client
  turn, with the tool catalogue, a "did you mean" hint and a correct example for that exact
  tool; only an unsalvageable turn ends it, as a retryable error rather than an answer.
- **A bare JSON call after prose is a call.** Text-only models routinely write
  "Now I'll fix that." and then a plain `{"name": "Edit", "arguments": {...}}` with no
  wrapper. That used to leak into the answer and the edit never happened. It is now
  recognized wherever it starts a line, streamed or not, including the OpenAI
  `{"type": "function", ...}` shape, calls to tools the client never registered - a real
  call to a missing tool has to be rejected with guidance, not printed - and an object with
  no name at all that fits exactly one tool's schema and no other.
- **Near-miss arguments are repaired, not rejected.** A doubled `{"arguments": {...}}`
  envelope, `cmd` for `command`, `old_str` for `old_string`, and extra keys a real tool API
  would ignore are fixed once instead of costing three identical retries.
- **An empty sample is re-asked.** An empty upstream reply made the client print "the model
  returned an empty response" and stop; so did prose that only *talked* about the call it
  failed to make.
- **The loop guard talks to the model, not the user.** A repeated call is now answered with
  "you already did that, the result will not change"; only a model that still will not move
  on gets the guard's own text, which is what bounds the loop.
- Incremental UTF-8 decoding preserves Unicode and DeepSeek DSML markers split across
  network reads. `read1()` avoids waiting for a full buffer before forwarding an SSE event.
- Multiline SSE data, CRLF boundaries and EOF are handled as complete events. Broken
  upstream streams produce protocol errors, not successful-looking assistant replies.
- Literal `</tool_call>`, `<arg>` and DSML markup inside JSON arguments remain data rather
  than being mistaken for syntax or silently rewritten.
- A fabricated `<tool_result>` discards the entire dependent continuation, including
  additional calls that relied on a tool result which never existed.
- Unterminated JSON strings are not invented. Bare JSON salvage is held back until it can
  be classified, avoiding visible JSON followed by a duplicate tool call.
- `tool_choice=none`, named tool choice, schema validation and call limits are enforced
  before calls reach clients—not merely requested in the model prompt.
- Inbound request validation returns 400/413/408 for invalid, oversized or timed-out
  bodies. Chunked requests share the size limit and reject ambiguous/truncated framing.
- Server instances use their own configuration. Model-map syntax matches the docs.
- Anthropic streams report input usage; non-streaming repair attempts accumulate usage.

## Endpoints

| Route | Purpose |
| --- | --- |
| `POST /v1/messages` or `/messages` | Anthropic Messages |
| `POST /v1/messages/count_tokens` or `/messages/count_tokens` | Approximate input token count |
| `POST /v1/chat/completions` or `/chat/completions` | OpenAI Chat Completions |
| `GET /v1/models` or `/models` | Aliases and configured upstream model IDs |
| `GET /health` or `/healthz` | Liveness and non-secret configuration |

The OpenAI **Responses API** and legacy text `/v1/completions` are not implemented.
In particular, this is not a claim of Codex CLI compatibility.

## Emulated wire format

```text
<tool_call>
{"name":"Read","arguments":{"file_path":"/tmp/example.txt"}}
</tool_call>
```

The parser also handles common tag aliases, raw XML-style arguments, single-quoted
pseudo-JSON, trailing commas, Python literals, and missing structural closing tags.
DeepSeek's fullwidth `｜｜DSML｜｜` markup is recognized at syntax boundaries without
normalizing literal argument content.

**`EMU_USE_STOP` now defaults to `false`.** A literal `</tool_call>` stop sequence can cut
file content mid-string. Opt in with `EMU_USE_STOP=true` only when that trade-off is
acceptable; the missing close-tag recovery remains available.

## Tool policy and loop protection

Loop state is reconstructed from each request's transcript, not shared between users:
identical-call fingerprints, repeat warnings, hard repeat limits, oscillation detection,
a conversation tool-round budget, and a per-turn call cap.

With `EMU_PARALLEL=false`, at most one call is forwarded. With it enabled, the configured
per-turn cap applies; a client's `parallel_tool_calls=false` or Anthropic
`disable_parallel_tool_use=true` can still disable parallel calls.

Rejected calls are retried up to three times with corrective instructions when
`EMU_LOOP_RETRY=true`, on the streaming path as well as the non-streaming one, inside the
same client turn. The corrective prompt restates the available tool names, suggests the
closest match and shows a correct call for the tool that failed.

Invalid calls stay blocked. What a client receives when every attempt fails is a **retryable
5xx**, not an assistant message: a coding agent treats any successful message as the
finished answer, so a proxy diagnostic delivered that way ends the task with the work
undone. Streaming turns hold their first event until the turn is known to be usable, so an
unusable turn fails before a single byte is written and the client simply asks again.
Deliberate policy outcomes - `tool_choice=none`, the repeat limit - are still explained in
text, because there the client asked for prose. An unsatisfied required/named tool choice
returns a protocol error.

Schema validation supports common recursive keywords: types, local `#/...` references,
properties/required/items, additional and pattern properties, enum/const, combinators,
common numeric/string/array bounds, and unique items. It is a bounded **subset of JSON
Schema**, not a complete validator; unsupported keywords and external references are not
implemented. Client permissions remain the authority for executing tools.

## Configuration

| Variable | Default | Meaning |
| --- | --- | --- |
| `EMU_HOST` / `EMU_PORT` | `127.0.0.1` / `8787` | Listen address |
| `EMU_UPSTREAM_BASE_URL` | `https://api.deepseek.com` | OpenAI-compatible backend |
| `EMU_UPSTREAM_PATH` | `/chat/completions` | Upstream route |
| `EMU_UPSTREAM_API_KEY` | — | Falls back to `DEEPSEEK_API_KEY` |
| `EMU_MODEL_BIG` / `EMU_MODEL_SMALL` | `deepseek-v4-pro` / `deepseek-flash` | Main and small-model targets |
| `EMU_MODEL_MAP` | — | Comma-separated `from=to` pairs or a JSON object |
| `EMU_MAX_TOOL_ROUNDS` | `25` | Tool-calling turns before forcing an answer |
| `EMU_MAX_REPEAT` | `3` | Identical-call hard limit |
| `EMU_MAX_CALLS_PER_TURN` | `4` | Cap when parallel calling is enabled |
| `EMU_PARALLEL` | `false` | Otherwise enforce one call per turn |
| `EMU_USE_STOP` | `false` | Opt-in closing-tag stop; can truncate literal code |
| `EMU_LOOP_RETRY` | `true` | Bounded non-streaming corrective retries |
| `EMU_SALVAGE` | `true` | Recover bare JSON calls |
| `EMU_MAX_RESULT_CHARS` | `24000` | Middle-truncate long tool results |
| `EMU_MAX_REQUEST_BYTES` | `16777216` | 16 MiB limit for length and chunked bodies |
| `EMU_CLIENT_TIMEOUT` | `30` | Client socket timeout in seconds |
| `EMU_TIMEOUT` / `EMU_MAX_RETRIES` | `300` / `3` | Upstream timeout and connection attempts |
| `EMU_LOG` / `EMU_LOG_BODIES` | `info` / `false` | Logging; bodies also dump each raw model reply, so they can contain sensitive prompts |

```bash
EMU_MODEL_MAP='my-model=deepseek-v4-pro,tiny=deepseek-flash' \
  python3 -m emutools --port 9000 --max-repeat 2 --max-tool-rounds 15
```

## Tests and single-file deployment

`emutools.py` at the repository root **is** the single-file build, committed and kept
current automatically: CI rebuilds it on every push to the default branch, verifies it with
its own self-test, and commits it back when it differs. Nobody has to run the builder.

```bash
python3 emutools.py                       # dependency-free deployment, no build step
python3 emutools.py --selftest            # 203 built-in checks, standalone

python3 -m unittest discover -s tests -v  # 109 regressions, including real sockets
python3 -m emutools --selftest            # the same 203 checks from the package
python3 build_single_file.py              # only needed to refresh it locally
python3 build_single_file.py --check      # what CI uses to catch a stale file
```

CI runs the package and standalone suites on **Python 3.9, 3.12 and 3.13**. A separate
job downloads pinned official Claude Code and OpenCode binaries and runs actual MCP,
read/edit and shell-test operations against a deterministic local model. That job needs
no API secret; it does not measure the behavior of a real LLM.

For opt-in **paid DeepSeek V4 Pro** testing with an installed client:

```bash
export EMU_UPSTREAM_API_KEY=sk-...
python3 scripts/live_cli_smoke.py --client claude --cli "$(command -v claude)" \
  --out-dir /tmp/emutools-claude-smoke
# Or: --client opencode --cli "$(command -v opencode)"
# Use --mock-upstream to exercise the client/tools without paying for a model.
```

The output directory must not already exist. The runner uses a disposable workspace,
small output/turn limits, a wall-clock timeout and narrow tool permissions. The real key
is supplied only to the proxy, not to the CLI or its stdio MCP process. Live costs are
not the same as a client's estimate for the advertised Claude alias.

A harder opt-in job runs Claude Code end to end against a real text-only model: fix three
real bugs in a module, call an stdio MCP tool for a token, and reproduce an adversarial
payload byte-for-byte - one that contains a literal `</tool_call>`, `<arg name=...>`, DeepSeek
DSML markers, Windows paths, Cyrillic and an emoji. It passes only if an independent test
run passes, the test file is untouched, the MCP tool really ran, and the client reported
success:

```bash
export EMU_UPSTREAM_API_KEY=sk-...
python3 scripts/hard_job_smoke.py --cli "$(command -v claude)" \
  --out-dir /tmp/emutools-hard --model deepseek-flash
```

See [the September 2026 test report](docs/testing-2026-09-05.md) for observed results and
limits, including the small VPS's inability to start OpenCode under memory pressure, and
[the hard-job report](docs/hard-job-2026-09-11.md) for what the live runs found.

## Layout

| Path | Contents |
| --- | --- |
| `emutools/core.py` | Configuration, canonical types, utilities |
| `emutools/protocol.py` | Prompts, tolerant parsing, incremental tool parser, validation |
| `emutools/wire.py` | Loop state, upstream HTTP/SSE, request translation |
| `emutools/engine.py` | Policy enforcement, turns and response serialization |
| `emutools/server.py` | HTTP framing, routes, request validation |
| `emutools/selftest_*.py` | 203-check built-in suite |
| `tests/` | Focused regressions and real-socket tests |
| `scripts/live_cli_smoke.py` | Opt-in real-model or deterministic-model CLI test |
| `scripts/hard_job_smoke.py` | Opt-in end-to-end hard job: real client, real model, MCP |
| `emutools.py` | Generated single-file build, refreshed automatically by CI |
| `scripts/cli_mock_upstream.py` | Deterministic model for real-client CI |
| `build_single_file.py` | Standalone distribution builder |

## Remaining limitations

- Emulation adds prompt tokens and depends on the model following a text protocol.
- Validation and repair are defensive heuristics, not a guarantee that a tool call is safe.
- Argument repair guesses when the intent is unambiguous (one stray key, one missing
  required key). A model that misnames two parameters at once is still rejected.
- The hard job is a real, cheap model doing real work, so it is not deterministic. The
  proxy bugs it exposed are fixed and regression-tested; the model's own coding mistakes
  are not something a proxy can fix.
- Images, documents and audio are replaced with text placeholders; this is a text-only bridge.
- Token counting is approximate when the upstream does not report usage.
- Raw XML argument form cannot unambiguously represent its own argument-closing delimiter;
  JSON is preferable for arbitrary source code.
- No Responses API, native upstream tool-call streaming, or authenticated public serving.

## License

MIT — see [LICENSE](LICENSE).
