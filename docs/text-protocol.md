# Text emulation: format, recovery, and context limits

The default generation instruction requests one format consistently:

```text
<tool_call>
{"name":"Read","arguments":{"file_path":"app.py","offset":120,"limit":60}}
</tool_call>
```

Use an exact declared tool name, a JSON object for arguments, and JSON-escaped source strings. Independent calls may appear in adjacent blocks when both the proxy and client allow batching. Stop after the last permitted block, not the first block of an allowed batch. Historical call/result IDs are correlation metadata, not syntax to copy into new calls.

The proxy translates these text blocks to the client's native tool channel. It does not send native tool definitions upstream or execute tools itself. The client and its permissions remain responsible for execution.

## Defensive compatibility, not guessed actions

Legacy parsing tolerates documented tag aliases, raw named XML parameters, and some recoverable JSON punctuation. Tolerance does not permit arbitrary interpretations:

- Conflicting outer/inner names, conflicting name aliases, and conflicting argument aliases are rejected.
- Wrapped arguments accompanied by additional flat argument fields are rejected instead of silently choosing a path or value.
- Duplicate JSON keys, including nested keys, duplicate XML attributes/parameters, and non-finite numeric literals are rejected.
- An exact declared name wins. Case/punctuation normalization resolves only a unique candidate; underscores are preserved.
- Literal source text inside arguments is data, including Unicode, quotes, and closing-tag-looking strings.
- Incomplete source strings and missing names are not invented. Schema, tool-choice, repeat, round, and batch limits still apply.

Malformed reserved wrappers with no usable call receive bounded recovery in synchronous and streaming requests. Once a call has been delivered, it is not replayed to repair a malformed peer. Ordinary prose is not turned into a tool call simply because the task is unfinished.

### Observed JSON-name/XML-parameter hybrid

One model repeatedly produced this malformed mixture:

```text
<tool_call>
{"name": "Read">
<parameter name="file_path">app.py</parameter>
<parameter name="offset">120</parameter>
<parameter name="limit">60</parameter>
</invoke>
```

The compatibility parser now recognizes this narrow grammar when the quoted name is complete, exactly declared, and consistent with any outer name. The body must consist entirely of one or more explicitly named, fully closed raw parameters. Duplicate/conflicting declarations, unexplained extra content, missing parameter closures, and name-only headers are rejected.

For this grammar, an alternate `invoke`/`tool_call` ending is a boundary only after the complete parameter list. A closing-looking string inside a parameter cannot terminate the call. This also permits an independently delimited following call without merging arguments between peers.

This is not a recommendation to generate hybrids. Flat JSON is preferred. Raw XML parameters cannot unambiguously represent their own argument-closing delimiter; use JSON strings for arbitrary source code.

Regression coverage includes synchronous decoding, streaming widths of 1, 2, 7, 64 and whole-response chunks, mixed-format peers, literal source strings, schema/call-limit enforcement, and no replay. See `tests/test_hybrid_text.py` and `tests/test_ambiguous_calls.py`. Exact private captures were also replayed offline without executing their recovered calls.

## Optional older-result text limits

```bash
EMU_MAX_RESULT_CHARS=8192 EMU_HISTORY_RESULT_CHARS=512 python3 -m emutools
```

Set the upstream credential separately. `EMU_HISTORY_RESULT_CHARS=0` is the default and preserves prior behavior. A positive integer applies an additional per-result text limit only to observations before the latest assistant tool-call batch.

- The latest batch keeps the normal `EMU_MAX_RESULT_CHARS` limit, including results split across messages or completed in reverse order.
- Older results keep their actual IDs, names and error status. Text omissions remain explicitly marked.
- Tool-call arguments, user text, original canonical history, and loop-protection inputs are not rewritten or discarded.
- Image bytes/URLs remain intact when image forwarding is separately enabled. This option is not an image-token budget.
- If no assistant tool-call boundary exists, the proxy does not guess which group is older.
- A positive history limit cannot enlarge an existing positive per-result limit. It also works when current-result text is configured as unlimited.

This is lossy context management, not a guarantee of better model behavior. Important older details may require a targeted reread. It is a character allowance, not an exact token/byte count or an aggregate conversation limit; omission markers and serialization add overhead. It does not change financial reservation rules or tool permissions. Coverage: `tests/test_history_limits.py`, also exercised against the generated standalone file.

## Full-stack benchmark controls

The paid runner supports:

- `--max-result-chars 1..24000`: normal per-result text allowance, default 24000.
- `--history-result-chars 0..24000`: optional older-result allowance, default 0.
- `--compact-continuation`: shorter initial instructions, requiring `--resume-app` and copied model-authored source. The complete contract remains in `REQUIREMENTS.md`; all acceptance requirements remain mandatory.

The runner records these choices, passes them to the actual proxy configuration, and rejects invalid numeric allowances before creating a workspace. A compact continuation still requires the initial independent MCP schema/profile batch, a representative query, real build/test/server/HTTP work, and the independent evaluator. The evaluator, seed, MCP implementation, and conservative spending guard are not weakened by these options.

A client exit code of zero or a successful parser test does not establish completion of the app task. Inspect actual native-client actions and independent application outcomes separately.
