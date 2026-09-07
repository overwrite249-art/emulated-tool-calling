# Image forwarding and real-client follow-up — September 7, 2026

## Status

- **154 regression tests pass**, including real-socket protocol tests and new history/counter regressions.
- **200 package self-checks and 200 standalone self-checks pass**, with exit code zero for each.
- The Python 3.9/3.12/3.13 and actual-client deterministic-model CI checks are green on the tested code.
- Opt-in image forwarding is implemented. Actual Claude Code has read synthetic image files and sent their bytes through emutools to the real vision model.
- A real parallel-result association bug was found and fixed in text-mode history. It has regression coverage, including reversed completion order. A later live check verified one image's originating-call association, but the **complete post-fix batched vision workflow has not passed**.
- **The larger V4 Pro full-stack challenge still has not passed.** The checked-in app has not been repaired and is not production-ready.

This updates, rather than rewrites, the [September 6 baseline](fullstack-and-vision-2026-09-06.md). Its image-omission statement describes the earlier source/default behavior. Current forwarding requires explicit opt-in; see [image inputs](image-inputs.md).

## Code and offline evidence

The image implementation added `Config.image_inputs` / `EMU_IMAGE_INPUTS`, ordered image content in canonical messages, validation/normalization, support for image-bearing tool results, JSON-mode preservation, and standalone bundling. Images remain opt-in and upstream-dependent. Native upstream tool declarations are not sent.

The first image revision passed 147 regressions. Seven further tests cover real-result association and correct analysis of fragmented client traces. The pre-fix correlation baseline failed the two relevant tests; the final suite passed **154/154**. The latest complete published-source run took 5.126 seconds on the VPS. This is a local test duration, not a throughput benchmark.

The image HTTP matrix uses a deterministic upstream across two client protocols, streaming/non-streaming replies, and legacy/JSON mode. These cases establish conversion and protocol behavior, not image understanding. Invalid image inputs return 400 before upstream I/O. Existing tool-schema, call-choice, repeat, bounded-recovery and literal-source-preservation checks remain in place.

The tested standalone was built from the published source:

```text
212270 UTF-8 bytes
5256 lines
SHA256 f883b7c10d813d5d77c37efdbe0de44b3bf36d308ed5e8ed7af4f39e1679c3e3
```

No application replacement was written by the reviewer to obtain a passing score.

## Unchanged synthetic images

All live image checks below use the same two 640×420 bitmap-font PNGs as the earlier direct-provider baseline. Expected answers are not given in the client's prompt or image workspace.

| Fixture | Expected code | Blue circles | Triangle | PNG bytes |
| --- | --- | ---: | --- | ---: |
| A | D7K4 | 3 | Green | 2780 |
| B | R2M9 | 1 | Red | 2470 |

```text
A SHA256 42e1c6c68988a1cff2e2e5ce82be5c6b24d60e2e7b95dbc5d9a9f28c1e07288e
B SHA256 e3f11770dda5fde95784508b2c4e76acbb77e7bee5c80b7cf41bcfc490ed0b4d
```

These are tiny synthetic checks, not a general OCR or vision benchmark. Stylized bitmap-font legibility is a confounder. The earlier `D7K4` versus `0ZK4` failure is retained, not corrected by changing the answer key or image.

## Live proxy/client run 1: transport works, association fails

Source: `4d50c7ab9303beddf4c9bfba722fb859dc66d9d1`.

Path: actual HTTP clients / **Claude Code 2.1.261 → emutools → DeepSeek V4 Flash Vision experimental**. The native client is real; the model is the DeepSeek vision target, not a Claude model or mock.

| Case | Observed result |
| --- | --- |
| OpenAI Chat Completions, non-streaming | HTTP 200; both original PNG hashes preserved; two emulated `record_scene` calls in one response |
| OpenAI visual answers | A: `0ZK4`, 3, green; B: `R2M9`, 1, red. 1/2 exact answers; both shape/color answers correct |
| Anthropic Messages, streaming, synthetic tool-result history | HTTP 200; B's original PNG preserved; one emulated call; B answered exactly |
| Actual Claude image reads | Client exited 0 in 4.38 seconds; both image files were requested together and their original bytes reached the provider |
| Native final answer | A was assigned B's contents; B was assigned A's contents, including the `0ZK4` misread. Neither labeled answer was exact |

The HTTP tool-result fixture is deliberately synthetic history. It is not counted as actual tool execution. The separate native-client phase used the real Read tool with only the two image files available.

### The real correlation defect

The native client emitted **two Read calls in one assistant response**. Their results arrived in reverse order. Image bytes and order were preserved, but text-mode call history and result wrappers omitted the actual call IDs. Both results were simply labeled `Read`, making the file association ambiguous. The model then swapped the image labels.

The correction now preserves matching, quoted `history_id` labels on actual historical calls and results, including ordinary text results. A system reminder instructs the model to match IDs and originating arguments rather than completion position. This does not invent tool results, change arguments, or authorize additional calls. JSON-mode history already carried explicit IDs and remains structured.

### The benchmark counter defect

The first vision harness reported a largest batch of one because it counted each streamed assistant fragment independently. Inspection of the raw trace showed two distinct calls sharing the same assistant message. The new helper groups by message ID and deduplicates repeated call blocks.

An independent offline re-audit of the retained raw run confirmed:

- Largest same-response batch: **2**.
- Images checked: **2**.
- Images associated with their actual call IDs in upstream history: **0**, before the fix.
- Reverse result order: **observed**.

The original raw result was retained. Its old `workflow_pass` field measured read/transport progress, not correct image answers, and must not be interpreted as a complete vision pass. Current probes report transport, batching, correlation, shape answers and exact answers separately.

## Live proxy/client run 2: post-fix rerun not qualified

Source: `7d748421b5f3c1ca1ab7adeedebe20a777a1940a`.

The first OpenAI request timed out in the capture transport and returned 502. Its outgoing provider payload was **byte-for-byte identical** to the earlier successful OpenAI request. The Anthropic stream returned HTTP 200 but delivered no tool call. The native client exited 1 without any Read calls. Three requests were forwarded; two were still uncompleted in the meter when the bounded run ended. A local reservation guard also rejected another attempt.

The capture bridge buffers provider responses, so these observations do not establish the precise provider/network cause and are not a streaming-latency measurement. Socket timeouts are not strict inference deadlines. Unfinished requests retained their full cost reservations; they were not assumed to be free. **No successful post-fix native workflow or exact vision answer is claimed from this run.**

## Native-only run 3: partial correlation verified, batching requirement failed

Source: `ac21042a44ea9eb96ab9c5be5e2cfb581b83086c`.

This used no synthetic HTTP tool history and did not repeat the direct-provider fixtures. It allowed at most two upstream requests, so a successful two-image workflow required both reads in the initial batch and a final response after their results.

Claude instead emitted two **sequential** Read calls. The first image was forwarded with its correct originating call ID: the audit checked one image and matched one. The third model request was refused by the local two-request cap, so the second image was not forwarded for a final answer.

- Native client exit: **1**.
- Largest same-response batch: **1**.
- Both-read correlation and final visual answers: **not established**.
- Overall result: **failed**, not relabeled as a pass.

This request-cap stop was not evidence that the provider account had no funds. Neither the required batching nor the exact expected answers were relaxed.

## Full-stack continuation 12: still no repair

The same correlation-aware source was tested through **actual Claude Code 2.1.261 → emutools → live DeepSeek V4 Pro**. It used the unchanged, genuinely model-written run-5 source checkpoint, a fresh seeded SQLite database, and explicit reviewer feedback about the four known defects. The reviewer supplied no replacement application code.

The provider produced malformed nested tool markup, followed by an unsupported tool-name-as-XML dialect. No actual call was emitted to the client. There were two metered provider responses, both with usage, and the run ended after 6.36 seconds.

| Check | Result |
| --- | --- |
| Native exit code | 0 — not sufficient for success |
| Actual client tool calls / MCP calls | 0 / 0 |
| Declared task completion | No |
| Protected evaluator/proxy files | Unchanged |
| Application source repair | None |
| Outer independent checks | 30/31, variant 73 |
| Overall challenge | Failed |

The outer evaluator built the existing files; generated `dist/` assets are **not** evidence that Claude built or repaired the app. The 30/31 score is another one-shot result on the unchanged racy baseline, not an improvement over earlier scores.

The unresolved app defects remain Unicode-insensitive search, the missing stylesheet route, concurrent idempotency errors, and transfers that can overspend stock. The earlier repeated-concurrency baseline remains 62/152 passing, with 300 completed paired HTTP responses. No new passing stress run, model-authored test suite/README, completed native build/server/HTTP workflow, or browser QA is claimed.

Unsupported output dialects can still cause an automatic-tool-choice client to stop without invoking tools. Emulation and bounded repair do not guarantee that a model follows the required protocol. Do not invent missing calls or argument values to manufacture a pass.

## CI and reproduction

The following runs completed all ten checks across push/PR workflows on the tested code. They include Python 3.9/3.12/3.13 and actual Claude Code/OpenCode binaries against a deterministic local model, not paid-model qualification:

- https://github.com/overwrite249-art/emulated-tool-calling/actions/runs/34141723350
- https://github.com/overwrite249-art/emulated-tool-calling/actions/runs/34141719481

```bash
python3 -m unittest discover -s tests -v
python3 -m emutools --selftest
python3 build_single_file.py /tmp/emutools.py
python3 /tmp/emutools.py --selftest
```

For explicitly paid tests and their limits, see [image inputs](image-inputs.md) and the help for `benchmarks/fullstack/run.py`. Every paid output directory must be new. A local request or spend guard is not a provider-wallet check. Missing usage remains conservatively reserved.

Only synthetic data and code are published. API credentials, wallet balances, raw client conversations/captures, and private client identifiers are excluded. The published source was scanned for credential patterns; no matches were found. The application checkpoint remains labeled as incomplete.
