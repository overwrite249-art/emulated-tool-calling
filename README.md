# emutools

**Emulated tool calling for models that don't do it natively — with no client-side changes.**

A local proxy that speaks both wire protocols coding agents use, and translates them onto
any OpenAI-compatible backend *without using that backend's native tool-calling support*.

- **Anthropic Messages API** — `POST /v1/messages` (Claude Code CLI)
- **OpenAI Chat Completions** — `POST /v1/chat/completions` (opencode, aider, cline, ...)

Tool calls are **emulated**: schemas are rendered into the system prompt, the model answers
in plain text, and the proxy parses that text back into genuine `tool_use` / `tool_calls`
structures. The client believes it is talking to a native tool-calling model. The upstream
model never receives a `tools` field at all.

Zero dependencies. Python 3.9+. **200 self-tests, all passing, no network required.**

---

## Quick start

```bash
export EMU_UPSTREAM_API_KEY=sk-...        # or DEEPSEEK_API_KEY
python3 -m emutools
```

Point either client at it:

```bash
# Claude Code CLI
ANTHROPIC_BASE_URL=http://127.0.0.1:8787 ANTHROPIC_AUTH_TOKEN=dummy claude

# opencode / any OpenAI-compatible client
OPENAI_BASE_URL=http://127.0.0.1:8787/v1 OPENAI_API_KEY=dummy opencode
```

The client-side key is ignored — the proxy authenticates upstream itself. **No API key is
stored in this repository; it is read from the environment only.**

Verify everything without a network or a key:

```bash
python3 -m emutools --selftest
```

## Single-file deploy

The package layout exists so the code is reviewable. For deployment there is one file:

```bash
python3 build_single_file.py     # -> ./emutools.py
python3 emutools.py --selftest   # same 200 checks
python3 emutools.py              # start the proxy
```

That output is dependency-free and self-contained — scp it anywhere with Python 3.9+.

## Endpoints

| Route | Purpose |
| --- | --- |
| `POST /v1/messages` | Anthropic Messages API — Claude Code |
| `POST /v1/messages/count_tokens` | Token estimate (Claude Code calls this) |
| `POST /v1/chat/completions` | OpenAI Chat Completions — opencode |
| `POST /chat/completions` | Same, unprefixed |
| `GET /v1/models` | Model list for both clients |
| `GET /health` | Liveness probe |

Streaming and non-streaming are implemented on both APIs, including correct Anthropic SSE
framing (`message_start`, `content_block_start`, `input_json_delta`, `message_delta`,
`message_stop`) and OpenAI `tool_calls` deltas.

## The wire protocol given to the model

```
<tool_call>
{"name": "Read", "arguments": {"file_path": "/etc/hosts"}}
</tool_call>
```

The parser is deliberately forgiving, because real models are messy. It accepts:

- Tag aliases: `tool_call`, `tool-call`, `toolcall`, `function_call`, `tool_use`, `invoke`
- XML-style args: `<parameter name="x">value</parameter>` instead of JSON
- Attribute style: `<tool_call name="Read">`
- Markdown fences around or inside the block
- Single quotes, trailing commas, unquoted keys, Python `True` / `None`
- A missing closing tag, because the stop sequence cut the response off
- **Vendor-native markup leaking into the text channel** (see below)

With `EMU_USE_STOP` on, the proxy sends `stop: ["</tool_call>"]` upstream so the model
cannot ramble past a call, then repairs the missing closing tag.

### Anti-hallucination

The most common failure of emulated tool calling is the model **writing the tool result
itself** and then reasoning against imaginary data. The proxy strips any `tool_result` the
model emits, discards everything after it, and the prompt forbids it explicitly. Covered by
tests in both the batch and streaming paths.

## Loop protection

Five independent mechanisms, because "it must not spin forever" was the hard requirement:

1. **Identical-call detection** — args are canonicalised (sorted keys, normalised
   whitespace) and fingerprinted; repeats are counted across the whole conversation.
2. **Escalating nudge** — one call *before* the limit, a warning is injected telling the
   model it already ran that exact call and must use the result it has.
3. **Hard block** — at `EMU_MAX_REPEAT` the call is refused and the client is told why, in
   plain language, so the agent can recover instead of stalling.
4. **Round budget** — `EMU_MAX_TOOL_ROUNDS` counts tool-calling turns; on exhaustion the
   proxy forces a final text-only answer by re-asking with tools disabled.
5. **Per-turn cap** — `EMU_MAX_CALLS_PER_TURN` stops one response fanning out into dozens.

The critical detail: **it never returns an empty response.** Every terminal state produces
real text, which is what stops the *client* from starting a retry loop of its own.

The proxy is stateless, like the APIs it emulates: loop state is rebuilt from the
transcript on every request, so protection survives client restarts and works correctly
with many interleaved conversations.

## Configuration

Every setting is an environment variable; the important ones also have CLI flags.

| Variable | Default | Meaning |
| --- | --- | --- |
| `EMU_HOST` / `EMU_PORT` | `127.0.0.1` / `8787` | Listen address |
| `EMU_UPSTREAM_BASE_URL` | `https://api.deepseek.com` | Any OpenAI-compatible backend |
| `EMU_UPSTREAM_API_KEY` | — | Falls back to `DEEPSEEK_API_KEY` |
| `EMU_MODEL_BIG` / `EMU_MODEL_SMALL` | `deepseek-v4-pro` / `deepseek-v4-flash` | Targets for opus/sonnet vs haiku |
| `EMU_MODEL_MAP` | — | Explicit `from=to` overrides, comma separated |
| `EMU_MAX_TOOL_ROUNDS` | `25` | Tool rounds before a forced answer |
| `EMU_MAX_REPEAT` | `3` | Identical calls before blocking |
| `EMU_MAX_CALLS_PER_TURN` | `4` | Calls allowed in one response |
| `EMU_PARALLEL` | `false` | Allow multiple calls per turn |
| `EMU_USE_STOP` | `true` | Send the stop sequence upstream |
| `EMU_SALVAGE` | `true` | Recover malformed calls |
| `EMU_MAX_RESULT_CHARS` | `24000` | Middle-truncate huge tool results |
| `EMU_TIMEOUT` / `EMU_MAX_RETRIES` | `300` / `3` | Upstream timeout and retry budget |
| `EMU_LOG` / `EMU_LOG_BODIES` | `info` / `false` | Log level and body dumps |

```bash
python3 -m emutools --port 9000 --max-repeat 2 --max-tool-rounds 15 --log debug
```

## A real bug this found

Streaming against the live API with a deliberately weak system prompt, the model ignored
the requested format and emitted **its own native tool-call markup into the text channel**,
using fullwidth `U+FF5C` sentinels:

```
<｜｜DSML｜｜tool_calls>
<｜｜DSML｜｜invoke name="Read">
<｜｜DSML｜｜parameter name="file_path" string="true">/etc/hosts</｜｜DSML｜｜parameter>
</｜｜DSML｜｜invoke>
</｜｜DSML｜｜tool_calls>
```

A naive parser leaks all of that to the user as garbage **and loses the tool call**. Fixes:
a dialect normaliser that rewrites vendor markup into canonical tags in both the batch and
streaming paths, and a prompt rule forbidding the built-in tool-call channel. The exact SSE
deltas the API really sent are now a permanent regression fixture.

Writing that fix introduced a subtler bug the char-by-char streaming test caught at once:
the normaliser matched a marker while its **second** sentinel character was still in flight,
stranding a pipe and corrupting the tag. A lookahead requiring the next tag character fixed
it.

## Tests

```
python3 -m emutools --selftest
...
All 200 checks passed.
```

| # | Section | Covers |
| --- | --- | --- |
| 1 | Parser happy paths | Every accepted tag and argument dialect |
| 2 | Malformed output | Broken JSON, missing tags, junk around the block |
| 3 | Escaping hazards | Nested quotes, backslashes, newlines, unicode |
| 4 | Hallucinated results | Model inventing tool output — batch and streaming |
| 5 | Schema validation | Unknown tools, missing required args, type coercion |
| 6 | Streaming fuzz | Splits 1–39 plus 300 random split patterns |
| 7 | Loop protection (unit) | Fingerprinting, counting, nudging, blocking |
| 8 | End-to-end HTTP | Real sockets against a mock upstream, both APIs |
| 9 | Multi-turn | Full agent sessions with several tool calls |
| 10 | Loop protection (e2e) | A model that genuinely will not stop calling |
| 11 | Error handling | Upstream 500s, timeouts, bad JSON, oversized results |
| 12 | Endpoints and edges | Models, health, token counting, empty tools |
| 13 | Concurrency | 16 simultaneous requests, no exceptions |
| 14 | Real captured output | Actual upstream bytes, including the DSML dialect |
| 15 | Multi-conversation isolation | 16 interleaved conversations, zero state bleed |

Sections 8–15 start a real proxy and a real mock upstream on loopback sockets, so they
exercise genuine network I/O rather than mocked function calls.

## Layout

| Path | Contents |
| --- | --- |
| `emutools/` | The package, split by concern |
| `emutools/protocol.py` | Prompt construction, tool schema rendering |
| `emutools/parsing.py` | Tolerant tool-call parser, dialect normaliser |
| `emutools/streaming.py` | Incremental parser that never leaks partial tags |
| `emutools/loops.py` | Loop detection and budgets |
| `emutools/translate.py` | Anthropic and OpenAI requests to one canonical form |
| `emutools/engine.py` | The turn loop |
| `emutools/server.py` | The HTTP layer |
| `emutools/selftest_*.py` | The 200-check suite |
| `build_single_file.py` | Rebuilds the standalone `emutools.py` |

## Known limitations

- Emulated calling costs prompt tokens: schemas ride along as text on every request. With
  Claude Code's full toolset, expect roughly 800–2,000 extra prompt tokens per turn.
- Weak models still occasionally malform a call. The parser salvages most cases, and a
  malformed call degrades to visible text rather than a crash — but it is not magic.
- `EMU_PARALLEL` is off by default. Most emulated models handle one call per turn far more
  reliably than several.
- Image and document content blocks become text placeholders, since the upstream is
  text-only.

## License

MIT — see [LICENSE](LICENSE).
