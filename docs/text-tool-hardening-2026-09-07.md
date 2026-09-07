# Text-tool hardening and live Claude qualification — 2026-09-07

## Status

**The proxy hardening is tested; the harder model-authored app workflow has not passed.** Vision experiments are parked. This report distinguishes parsing/transport tests, actual native-client tool activity, and application completion.

Verified code snapshot: `c2796f9c20117861bda596e9f594bc7a8eabe558`. Documentation-only commits may follow it. The test setup used the actual Claude Code 2.1.261 binary, emutools text emulation, and live `deepseek-v4-pro`. No native tool definitions were sent upstream.

## Changes completed

1. **Malformed text-wrapper recovery.** Reserved wrapper attempts with no usable call enter bounded recovery in synchronous and streamed requests, including when preceded by prose. Fenced wrapper examples and ordinary mentions are not treated as missing actions.
2. **Ambiguity and literal guards.** Conflicting call names, conflicting argument aliases, wrapped arguments plus extra flat argument fields, duplicate JSON keys/XML attributes/XML parameters, and non-finite literals are rejected. Name normalization resolves only a unique candidate. Actual string contents are retained.
3. **Consistent generation instructions.** The default prompt requests flat JSON call blocks rather than advertising conflicting raw-XML instructions. An independent batch stops after its final permitted block.
4. **Observed hybrid compatibility.** A complete explicit JSON name followed by fully closed named XML parameters can be decoded through a narrow grammar. No missing name, unfinished parameter value, or name-only header is turned into a call. Alternate closing tags are recognized only at validated boundaries.
5. **Real-socket text-policy tests.** Anthropic/OpenAI, synchronous/streaming tests cover recovery before delivery, no replay or retargeting of a valid peer, and literal arguments in independent batches. These use deterministic upstream responses, not a paid model.
6. **Optional context controls.** The benchmark has validated current-result and older-result allowances, plus an opt-in compact source-only continuation. Older-result clipping is rendering-only: the latest batch retains its normal allowance, actual IDs/error status remain present, and original history/arguments/loop inputs are not mutated. The full app contract stays available on disk.

See [the text protocol guide](text-protocol.md) for exact boundaries, defaults, and trade-offs. Older-result clipping is lossy and may require targeted rereads; it is not proven to improve completion on every model.

## Verification

- **213 regression tests passed** on the published code snapshot, including real TCP cases. The last VPS run completed in 7.405 seconds.
- **200 package self-checks and 200 standalone self-checks passed**, including parser fuzzing and deterministic end-to-end checks.
- The eight older-result tests also passed against the generated standalone implementation with the feature enabled.
- The new hybrid tests were red before implementation: 12 tests, five failures and two errors. Older-result tests were red before implementation: eight errors. Runner-option tests were red before implementation: two failures.
- The history publication differed from the original tested local files only by one trailing blank line per file. That difference was inspected, then the actual published bytes were tested again. Later runner bytes were verified exactly against the tested patch.
- Python 3.9, 3.12 and 3.13 plus actual Claude Code/OpenCode binaries passed all ten CI checks for the code snapshot. The real-client CI upstream is deterministic: these jobs are **not** live-model qualification.

CI references:

- https://github.com/overwrite249-art/emulated-tool-calling/actions/runs/34158090391
- https://github.com/overwrite249-art/emulated-tool-calling/actions/runs/34158087011

Generated standalone for the current production code:

```text
221173 UTF-8 bytes; 5458 lines
SHA-256 59fb496d8c90868872890f6781b2a90a5171e789d8d670a4fb22595cf2034403
```

A credential-pattern scan of 64 text files in the code snapshot returned no matches. This is a scoped pattern scan, not an exhaustive proof that arbitrary source is free of secrets. Credentials, wallet data, private client IDs, and raw captures are excluded from public artifacts.

### Exact offline capture replays

The prior ambiguous two-Read response now retains the unambiguous app-file Read and rejects the peer containing conflicting file paths. The hybrid responses from the following run recover respectively one Read, two independent Reads, and a hybrid Read followed by a conventional XML Read. Their incomplete name-only peer recovers zero calls. Streaming widths 1, 2, 7, 64 and whole-response chunks were checked.

These were decoder replays using the captured text and stated test schemas. **No recovered call was executed by the offline replay.** They are not substitutes for native-client/model execution.

## Actual live text-only continuations

All runs below started from the same earlier genuinely model-authored source checkpoint, with fresh disposable databases and disclosed reviewer feedback. The reviewer did not supply replacement app code. The full contract, seed, read-only MCP implementation, independent evaluator, repeated-concurrency evaluator and financial guard were not relaxed.

| Run | Native tool calls | Largest response batch | MCP calls / errors | MCP overlap | Independent checks | Outcome |
| --- | ---: | ---: | --- | --- | --- | --- |
| 13 | 8 | 3 | 5 / 0 | Yes | 29/31 | Local spend-reservation guard; no app edits |
| 14 | 9 | 3 | 6 / 1 | Yes | 28/31 | Hybrid syntax rejected before local guard; no app edits |
| 15 | 7 | 3 | 3 / 0 | Yes | 29/31 | No formatting retry observed; local guard; no app edits |
| 16 | 4 | 3 | 3 / 0 | Yes | 29/31 | Ordinary non-completion reply; no app edits |
| 17 | 4 | 1 | 3 / 0 | No | 28/31 | Sequential calls, malformed reply and local guard; no app edits |

MCP overlap is separate from multiple calls in one assistant response. The MCP harness includes a disclosed 0.6-second instrumentation delay. Neither measurement establishes concurrent HTTP correctness in the application.

### Run 14

Used a 4096-character result allowance and 2400-token response allowance. It produced schema/profile together, a query plus Bash, three queries together, and two individual Reads. One MCP result was a real error because multiple SQL statements were placed in one query call; the subsequent separate queries worked. A Bash directory listing also failed because the tests directory did not exist. Nine results does not mean nine successful operations.

The repeatedly mixed JSON-name/XML-parameter syntax motivated the hybrid regression and decoder change. All eleven accepted provider requests had recorded usage. The stop was the local reservation guard, not evidence of provider-account depletion.

### Run 15

Used the published hybrid fix, compact source-only instructions, an 8192-character result allowance and a 2000-token response allowance. The actual client read the requirements, listed files, queried the database, and read backend/build source. It emitted seven native tool calls in three provider requests without an observed formatting retry, then hit the local pre-request reservation guard. It did not edit the app or run its own build/tests/server.

### Run 16

Added the optional 512-character allowance for older results while retaining the normal 8192-character current-result allowance. Five provider requests produced a three-call schema/profile/requirements batch and one representative query. The final provider response was simply `Group by tool call`. The native client exited zero without claiming completion. No financial-guard rejection was recorded for this run.

The final upstream request retained the actual task and correctly correlated the latest query result. Its role/message lengths and omission markers confirmed that the configured history clipping was active. This demonstrates context transformation, **not** successful app work. Ordinary prose was not converted into invented calls.

### Run 17

Requested thinking enabled with low reasoning effort, a 2500-token response allowance, 6144-character current results and 512-character older results. The actual upstream payload contained those generation settings. The model made schema, profile and query calls sequentially, then one Bash requirements/listing command. It did not satisfy the requested initial batch.

Its next Bash attempt had an incomplete JSON envelope and a mismatched plural closing tag. The attempted recovery was blocked by the local reservation guard. All five accepted requests had recorded usage. There were no app-source changes, native app build/test/server commands, or passing app qualification.

## What is still not established

The unchanged checkpoint still has known stylesheet-serving, Unicode-search and concurrent-transfer/idempotency defects. Its one-shot acceptance score varies because of race conditions; a higher count is not a repair. The existing repeated-concurrency baseline failed 90 of 152 checks. There is no repaired stress-test pass, model-written regression suite/README, completed native app build/test/repair workflow, or browser QA from these continuations.

The outer evaluator built and checked the unchanged checkpoint. That work is not attributed to Claude. All listed native-client runs have exited. No literal-perfection claim, live V4 OpenCode pass, full vision qualification, or inference that a local guard means account depletion is made. The PR remains draft.
