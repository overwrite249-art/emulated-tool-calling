# emutools

[![Tests](https://github.com/overwrite249-art/emulated-tool-calling/actions/workflows/tests.yml/badge.svg)](https://github.com/overwrite249-art/emulated-tool-calling/actions/workflows/tests.yml)

**Tool calling for coding clients through OpenAI-compatible backends, with optional image inputs.**

emutools is a dependency-free Python 3.9+ proxy. It accepts Anthropic Messages or OpenAI
Chat Completions, renders tool schemas into the prompt, and turns the model's text back
into native `tool_use` / `tool_calls` responses. It never sends a native `tools` field upstream.

- **Claude Code:** Anthropic Messages, including streaming and token counting.
- **OpenAI-compatible clients:** Chat Completions, including streaming tool arguments and usage.
- **MCP tools:** tools registered by the client are translated like other client tools; emutools is not itself an MCP server.
- **Optional images:** forward user images and image-bearing tool results to a compatible vision model. Disabled by default.

## Quick start

```bash
export EMU_UPSTREAM_API_KEY=sk-...  # or DEEPSEEK_API_KEY; never commit your real key
python3 -m emutools
```

The default upstream is `https://api.deepseek.com`, routing main models to
`deepseek-v4-pro` and small/fast aliases to `deepseek-v4-flash`.

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

- Incremental UTF-8 decoding preserves Unicode and DeepSeek DSML markers split across
  network reads. `read1()` avoids waiting for a full buffer before forwarding an SSE event.
- Multiline SSE data, CRLF boundaries and EOF are handled as complete events. Broken
  upstream streams produce protocol errors, not successful-looking assistant replies.
- Literal `</tool_call>`, `<arg>` and DSML markup inside JSON arguments remain data rather
  than being mistaken for syntax or silently rewritten.
- Recognized fabricated `<tool_result>` output at protocol boundaries discards the
  dependent continuation, including calls that relied on an observation which never existed.
- Unterminated JSON strings are not invented. Bare JSON salvage is held back until it can
  be classified, avoiding visible JSON followed by a duplicate tool call.
- Conflicting names or argument locations, duplicate JSON/XML declarations, and non-finite
  literals are rejected instead of silently selecting an interpretation.
- `tool_choice=none`, named tool choice, schema validation and call limits are enforced
  before calls reach clients—not merely requested in the model prompt.
- Inbound request validation returns 400/413/408 for invalid, oversized or timed-out
  bodies. Chunked requests share the size limit and reject ambiguous/truncated framing.
- Server instances use their own configuration. Model-map syntax matches the docs.
- Anthropic streams report input usage; streamed and non-streaming repair attempts accumulate usage.
- Streamed empty, rejected and nested-call responses receive bounded recovery without replaying delivered calls.
- Malformed reserved text wrappers receive bounded recovery; ordinary prose is not turned into invented calls.
- A narrow explicit-name/XML-parameter hybrid is recognized only at validated boundaries.
- Optional JSON envelopes preserve arbitrary source strings and apply the same local tool-policy guards.
- Opt-in image forwarding preserves image bytes rather than sending omission placeholders.
- Actual call/result identifiers remain in history, including when parallel calls to the
  same tool finish out of order. This applies to text results and image-bearing results.
- Optional older-result text limits reduce retained observations without changing call
  arguments, current-batch allowances, original canonical history, or loop-guard inputs.

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

The default prompt requests this flat JSON format consistently. The parser also handles
common tag aliases, raw XML-style arguments, single-quoted pseudo-JSON, trailing commas,
Python literals, and some missing structural closing tags. DeepSeek's fullwidth
`｜｜DSML｜｜` markup is recognized at syntax boundaries without normalizing literal argument
content. Complete named JSON calls in bare DSML wrappers and a narrow observed hybrid
format are also recognized. Arbitrary invented dialects are not guaranteed to work.

See [text format, recovery boundaries and context controls](docs/text-protocol.md).
Missing names, conflicting values and incomplete source strings are not guessed.

**`EMU_USE_STOP` now defaults to `false`.** A literal `</tool_call>` stop sequence can cut
file content mid-string. Opt in with `EMU_USE_STOP=true` only when that trade-off is
acceptable; the missing close-tag recovery remains available.

### Optional JSON response mode

`EMU_JSON_OUTPUT=true` requests provider JSON-object output instead of the default text-tag
format. This is still emulation: no native upstream tool definitions are sent. The full
JSON envelope is buffered before releasing content or calls, so first-content latency is
higher. A provider can still return empty or malformed content; local validation stays strict.

See [JSON mode and generation controls](docs/json-output.md) for the contract, limitations,
and `EMU_THINKING` / `EMU_REASONING_EFFORT` settings. JSON mode is opt-in, not a universal
recommendation for every model or workload.

### Optional image inputs

```bash
EMU_IMAGE_INPUTS=true EMU_PARALLEL=true EMU_THINKING=disabled \
  EMU_MODEL_BIG=deepseek-v4-flash-vision-exp \
  EMU_MODEL_SMALL=deepseek-v4-flash-vision-exp \
  python3 -m emutools
```

Set the upstream key separately as in Quick start. **Both the opt-in flag and a compatible
vision target are required.** Selecting a vision model alone does not enable forwarding.
Image support does not enable native upstream tool calling or guarantee correct OCR.

See [image inputs and their limits](docs/image-inputs.md) for supported formats, tool-result
correlation, privacy, and bounded native-client checks.

## Tool policy and loop protection

Loop state is reconstructed from each request's transcript, not shared between users:
identical-call fingerprints, repeat warnings, hard repeat limits, oscillation detection,
a conversation tool-round budget, and a per-turn call cap.

With `EMU_PARALLEL=false`, at most one call is forwarded. With it enabled, the configured
per-turn cap applies; a client's `parallel_tool_calls=false` or Anthropic
`disable_parallel_tool_use=true` can still disable parallel calls.

Empty, malformed or rejected outputs can be retried up to three times with corrective
instructions when `EMU_LOOP_RETRY=true`. Invalid calls remain blocked after the last attempt.
Streaming recovery stops once a valid call has been emitted; a delivered call is never
replayed to fix an invalid peer. Transport failures are not retried by that repair loop.
An unsatisfied required/named tool choice returns a protocol error.

Schema validation supports common recursive keywords: types, local `#/...` references,
properties/required/items, additional and pattern properties, enum/const, combinators,
common numeric/string/array bounds, and unique items. It is a bounded **subset of JSON
Schema**, not a complete validator; unsupported keywords and external references are not
implemented. Client permissions remain the authority for executing tools.

History labels connect prior calls and results by their actual identifiers, not by
completion order. They are metadata, not new tool arguments. Metadata cannot force a
model to reason correctly about an observation; check actual outputs independently.

### Optional older-result limits

```bash
EMU_MAX_RESULT_CHARS=8192 EMU_HISTORY_RESULT_CHARS=512 python3 -m emutools
```

The history setting defaults to `0` (disabled). A positive value additionally shortens
older tool-result text; the latest assistant tool-call batch keeps the normal per-result
allowance, even when its results are split across messages or finish out of order. IDs,
names, error status and image bytes remain intact. Omissions are marked, not replaced with
invented summaries. Original history and call arguments are not mutated.

This is lossy context management, not an aggregate token budget or a guaranteed model-quality
improvement. Important older details may need targeted rereads. See the
[text protocol guide](docs/text-protocol.md) for exact behavior and trade-offs.

## Configuration

| Variable | Default | Meaning |
| --- | --- | --- |
| `EMU_HOST` / `EMU_PORT` | `127.0.0.1` / `8787` | Listen address |
| `EMU_UPSTREAM_BASE_URL` | `https://api.deepseek.com` | OpenAI-compatible backend |
| `EMU_UPSTREAM_PATH` | `/chat/completions` | Upstream route |
| `EMU_UPSTREAM_API_KEY` | — | Falls back to `DEEPSEEK_API_KEY` |
| `EMU_MODEL_BIG` / `EMU_MODEL_SMALL` | `deepseek-v4-pro` / `deepseek-v4-flash` | Main and small-model targets |
| `EMU_MODEL_MAP` | — | Comma-separated `from=to` pairs or a JSON object |
| `EMU_MAX_TOOL_ROUNDS` | `25` | Tool-calling turns before forcing an answer |
| `EMU_MAX_REPEAT` | `3` | Identical-call hard limit |
| `EMU_MAX_CALLS_PER_TURN` | `4` | Cap when parallel calling is enabled |
| `EMU_PARALLEL` | `false` | Otherwise enforce one call per turn |
| `EMU_USE_STOP` | `false` | Opt-in closing-tag stop; can truncate literal code |
| `EMU_LOOP_RETRY` | `true` | Bounded streamed and non-streaming corrective retries |
| `EMU_JSON_OUTPUT` | `false` | Opt-in provider JSON-object mode; whole-envelope buffering |
| `EMU_IMAGE_INPUTS` | `false` | Opt-in image forwarding; requires a vision-capable upstream |
| `EMU_THINKING` | empty | Explicit `enabled` / `disabled`; empty preserves provider default |
| `EMU_REASONING_EFFORT` | empty | `low`, `medium`, `high`, `xhigh`, `max`; provider-dependent |
| `EMU_SALVAGE` | `true` | Recover bare JSON calls; in JSON mode, narrowly recover surplus closing brackets |
| `EMU_MAX_RESULT_CHARS` | `24000` | Text-only middle truncation; multimodal tool-result text prefix budget |
| `EMU_HISTORY_RESULT_CHARS` | `0` | Additional older-result text cap; 0 disables, latest batch keeps normal allowance |
| `EMU_MAX_REQUEST_BYTES` | `16777216` | 16 MiB limit for length and chunked bodies, including base64 images |
| `EMU_CLIENT_TIMEOUT` | `30` | Client socket timeout in seconds |
| `EMU_TIMEOUT` / `EMU_MAX_RETRIES` | `300` / `3` | Upstream timeout and connection attempts |
| `EMU_LOG` / `EMU_LOG_BODIES` | `info` / `false` | Logging; body dumps can contain sensitive prompts and images |

```bash
EMU_MODEL_MAP='my-model=deepseek-v4-pro,tiny=deepseek-v4-flash' \
  python3 -m emutools --port 9000 --max-repeat 2 --max-tool-rounds 15
```

## Tests and single-file deployment

```bash
python3 -m unittest discover -s tests -v  # 213 regressions, including real sockets
python3 -m emutools --selftest            # 200 built-in checks and parser fuzzing
python3 build_single_file.py /tmp/emutools.py
python3 /tmp/emutools.py --selftest       # same 200 checks, standalone
python3 /tmp/emutools.py                  # dependency-free deployment
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

### Bounded full-stack continuations

The full-stack runner is also paid. Choose a spending limit deliberately and a fresh
output directory. For an existing model-authored checkpoint:

```bash
python3 benchmarks/fullstack/run.py --cli "$(command -v claude)" \
  --out-dir /tmp/emutools-fullstack-check \
  --resume-app examples/claude-stockroom --compact-continuation \
  --thinking disabled --max-output-tokens 2000 \
  --max-result-chars 8192 --history-result-chars 512 --budget-usd 0.10
```

The compact mode keeps the full contract in `REQUIREMENTS.md`; it does not relax acceptance
checks. The evaluator, seeded MCP tools and conservative financial reservation guard are
unchanged. A reservation can reject a new request even when completed-request charges are
below the limit. That is not proof of provider-account depletion.

The small smoke test passed with the real model, but **the harder full-stack challenge
has not passed**. The inventory checkpoint is not production-ready. Read the dated
[smoke-test report](docs/testing-2026-09-05.md),
[full-stack/vision baseline](docs/fullstack-and-vision-2026-09-06.md),
[image-forwarding follow-up](docs/vision-and-correlation-2026-09-07.md), and
[text-tool hardening and live results](docs/text-tool-hardening-2026-09-07.md).
The latest text continuations still made no app-source repairs. Live OpenCode testing on
the small VPS was blocked by memory pressure; deterministic real-client CI is a separate result.

## Layout

| Path | Contents |
| --- | --- |
| `emutools/core.py` | Configuration, canonical types, utilities and rendering-only history limits |
| `emutools/protocol.py` | Prompts, tolerant parsing, incremental tool parser, validation |
| `emutools/media.py` | Opt-in image normalization, validation and correlated result rendering |
| `emutools/wire.py` | Loop state, upstream HTTP/SSE, request translation |
| `emutools/structured.py` | Opt-in strict JSON envelopes and consistent tool history |
| `emutools/engine.py` | Policy enforcement, turns and response serialization |
| `emutools/server.py` | HTTP framing, routes, request validation |
| `emutools/selftest_*.py` | 200-check built-in suite |
| `tests/` | Focused regressions and real-socket tests |
| `scripts/live_cli_smoke.py` | Opt-in real-model or deterministic-model CLI test |
| `scripts/cli_mock_upstream.py` | Deterministic model for real-client CI |
| `benchmarks/fullstack/` | Seeded SQLite/MCP challenge, bounded paid runner, independent acceptance and concurrency checks |
| `benchmarks/vision/` | Separate direct-provider, proxy-HTTP and actual native-client image checks |
| `examples/claude-stockroom/` | Model-authored checkpoint; read its provenance and known limitations before use |
| `build_single_file.py` | Standalone distribution builder |

## Remaining limitations

- Emulation adds prompt tokens and depends on the model following a text protocol.
- Validation and repair are defensive heuristics, not a guarantee that a tool call is safe.
- Optional older-result clipping can remove details the model later needs; it is not an exact token budget.
- Image forwarding is opt-in and upstream-dependent. Documents/audio and unsupported image source types are not implemented.
- System/assistant image inputs are not forwarded. Provider Files API references are not supported.
- Token counting is approximate, especially for multimodal input; do not use it as exact image billing or admission control.
- Raw XML argument form cannot unambiguously represent its own argument-closing delimiter;
  JSON is preferable for arbitrary source code.
- No Responses API, native upstream tool-call streaming, or authenticated public serving.
- A client exiting with code zero does not prove a model completed its task. The benchmark checks actual tool activity and independent outcomes.

## License

MIT — see [LICENSE](LICENSE).
