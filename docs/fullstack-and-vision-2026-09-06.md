# Real CLI full-stack and vision checks — September 6, 2026

## Bottom line

The proxy improvements pass **133 regression tests**, **200 package self-checks**, and **200 standalone self-checks**. Actual Claude Code and OpenCode binaries also pass the deterministic-model CI workflows.

**The full real-model coding challenge has not passed.** Do not treat the checked-in inventory application as production-ready. The vision provider can process images, but this version of emutools still omits image blocks, even when the selected target is a vision model.

## What was tested

The hard-work path was **Claude Code 2.1.261 → emutools → live DeepSeek V4 Pro**, using read-only MCP tools against a disposable, seeded SQLite database. No native upstream tool definitions were sent.

The task required database inspection, an actual TypeScript/Python inventory application, a real build and server, meaningful author-written tests and README, HTTP checks, and corrections based on independent failures. The first assistant response had to contain both independent schema/profile MCP calls. Later source-only continuations used the genuine model-written checkpoint, with reviewer feedback disclosed.

The original 31-check evaluator was not weakened. It created a fresh database with a different seed variant. Additional repeated-concurrency checks were introduced after an intermittent defect appeared.

### Three distinct kinds of concurrency

- **Model batching:** multiple tool calls in one assistant response, not merely several sequential turns.
- **MCP overlap:** overlapping execution of the schema/profile tools. Each has a disclosed 0.6-second instrumentation delay; this is not a database throughput benchmark.
- **Application concurrency:** separate simultaneous HTTP transfer requests against the running application. Successful tool batching does not prove application transactions are safe.

## Proxy fixes from the live failures

- Bounded recovery for empty, rejected, and nested streamed tool output, without replaying calls already delivered to the client.
- Explicit provider thinking/reasoning settings, rather than assuming a client-side thinking flag configures the upstream model.
- Opt-in JSON envelopes with consistent name-first call history and real-result correlation. No native upstream function definitions are used.
- Optional deletion of at most two mismatched surplus JSON closers. No missing names, argument values, or unterminated source strings are invented. Whole-envelope and schema/policy checks still apply.
- Streaming and non-streaming private diagnostics, excluding reasoning text, plus explicit local request-cap/reservation rejection reasons.
- Recognition of complete, named JSON calls inside bare DSML wrappers, with ASCII/fullwidth and chunk-boundary regressions. Literal wrapper strings inside arguments remain unchanged.

Exact captured malformed outputs were replayed offline to validate the fixes. The surplus-bracket replay preserved its argument values and passed schema validation. The bare-DSML replay recovered both original named Read calls in sync and chunked parsing. **Neither offline replay executed any generated command.**

## Actual application outcome

The application checkpoint was genuinely written by the real model through Claude Code in the earlier run 5. Five source files were created: the backend, build script, HTML, CSS, and TypeScript. The outer evaluator built and ran that checkpoint; these reviewer actions are not attributed to Claude.

Later continuations did not repair the five source files. Byte hashes remained unchanged. No Claude-authored application README or own test suite, completed native build/server/HTTP workflow, or browser QA was established.

| Run | Protocol / thinking | Actual client tool calls | Largest same-response batch | Independent checks | Outcome |
| --- | --- | ---: | ---: | --- | --- |
| 5 | Text / disabled | 15 | 2 | 29/31 | Wrote the genuine application checkpoint; incomplete workflow |
| 6 | Text / disabled, smaller output cap | 10 | 4 | 29/31 | No source repair |
| 7 | JSON / disabled | 8 | 2 | 29/31 | Malformed/empty output; no source repair |
| 8 | JSON / low thinking | 5 | 2 | 28/31 | Malformed output; no source repair |
| 9 | Revised JSON history / low thinking | 5 | 2 | 30/31 | No source repair; request cap reached |
| 10 | Text / disabled | 4 | 2 | 29/31 | Complete calls in bare DSML wrappers escaped recognition |
| 11 | Text / disabled, bare DSML fix | 13 | 2 | 28/31 | Advanced through more real reads/queries; still no source repair |

**The varying acceptance scores are not improvement.** The unchanged baseline contains nondeterministic concurrency bugs. Run 11 made eight actual MCP calls, with one query error, and observed MCP overlap; this is not a completed coding task.

### Confirmed remaining application defects

- Unicode case-insensitive search for `привіт` returns zero rows instead of three.
- The generated stylesheet is not served at `/style.css` (404).
- Concurrent same-key retries can return 200/500 rather than two successful identical responses with one effect.
- Competing distinct-key transfers can both spend the same available stock and inflate inventory totals.

A separate one-off 25-pair test saw 16 incorrect double-success outcomes. The formal repeated-concurrency baseline completed **300 paired-request responses**, with **62/152 checks passing**. Its 90 failures were: stylesheet 1, overspend 29, same-key retries 27, and conflicting-key reuse 33. Each race category was tested for 50 iterations. The missing-destination check passed.

The success response occurring before the transaction context commits was also identified in code review; no isolated commit-failure test is claimed for that observation.

## Vision model check

The account's model list included `deepseek-v4-flash-vision-exp`. The official guide documents OpenAI-compatible image blocks, PNG/JPEG/GIF/WebP support, and a maximum of 384 billed image tokens per image after resizing.

`benchmarks/vision/probe.py` builds two synthetic PNGs in memory with a bitmap-font code and colored shapes. It validates image structure separately, sends at most two paid requests, and has a conservative $0.02 peak-tariff cap. It needs no imaging packages and contains no credential.

This is a **direct provider baseline plus an offline inspection of emutools conversion**, not a Claude Code vision workflow and not a successful image request through the proxy.

| Fixture | Expected code | Returned code | Blue circles | Triangle color |
| --- | --- | --- | --- | --- |
| A | D7K4 | 0ZK4 | 3 — correct | Green — correct |
| B | R2M9 | R2M9 | 1 — correct | Red — correct |

Both replies were valid JSON and used the requested vision model. Exact responses passed **1/2** checks: shapes and colors were correct in both, but one stylized bitmap code was misread. Two images do not establish a general accuracy rate, and bitmap-font legibility is a limitation of this stimulus.

### Image conversion is currently unsupported

The diagnostic selected the vision model for both incoming protocols. In both cases:

- Upstream image blocks: **0**.
- An `[image omitted: ...]` placeholder was present.

Selecting a vision model does **not** enable image forwarding in this version. Do not present text-only responses from this configuration as image analysis. See the existing text-only limitation in the README.

## Reproduction

```bash
python3 -m unittest discover -s tests -v
python3 -m emutools --selftest
python3 build_single_file.py /tmp/emutools.py
python3 /tmp/emutools.py --selftest
```

For the opt-in paid visual baseline:

```bash
export EMU_UPSTREAM_API_KEY=YOUR_KEY
python3 benchmarks/vision/probe.py
```

For the full-stack runner's current options:

```bash
python3 benchmarks/fullstack/run.py --help
```

The runner requires an installed real client, a new output directory, and an upstream key. Its local request/spend guard can stop a run before the account is empty. A local HTTP 402 is not evidence of an exhausted provider wallet or a Notion/OpenAI billing problem.

### CI references

The following CI runs are green on the published code, including Python 3.9/3.12/3.13 and actual client binaries with a deterministic local upstream:

- https://github.com/overwrite249-art/emulated-tool-calling/actions/runs/34031938960
- https://github.com/overwrite249-art/emulated-tool-calling/actions/runs/34031937159

Those CI client runs do not replace live-model qualification.

### Privacy and provenance

Only synthetic application and image data were used. API keys, wallet balances, raw client conversations, private captures, and client identifiers are not committed. The model-written checkpoint remains labeled with its historical failure provenance. No reviewer-written application replacement is passed off as model work.

Provider documentation checked September 6, 2026:

- https://api-docs.deepseek.com/guides/vision
- https://api-docs.deepseek.com/guides/json_mode/
- https://api-docs.deepseek.com/guides/thinking_mode
- https://api-docs.deepseek.com/quick_start/pricing/
