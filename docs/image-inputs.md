# Opt-in image inputs

Image forwarding is disabled by default. `EMU_IMAGE_INPUTS=true` (or `Config(image_inputs=True)`) enables it for that proxy configuration. Selecting a vision model alone does not enable it. There is no separate `--image-inputs` CLI switch.

The upstream must support OpenAI-compatible image content arrays. Tool calling remains emulated with text responses: no native upstream tool definitions are introduced.

```bash
export EMU_UPSTREAM_API_KEY=YOUR_KEY
EMU_IMAGE_INPUTS=true EMU_PARALLEL=true EMU_THINKING=disabled \
  EMU_MODEL_BIG=deepseek-v4-flash-vision-exp \
  EMU_MODEL_SMALL=deepseek-v4-flash-vision-exp \
  python3 -m emutools
```

Keep the real credential in the proxy process, not the coding client's workspace or MCP configuration. The usual default V4 Pro target is not the experimental vision target.

## Supported input shapes

- OpenAI Chat Completions user `image_url` blocks, with a URL string or `{ "url": "...", "detail": "auto" }` object.
- Anthropic Messages user `image` blocks with `source.type=base64` and `media_type`/`data`, or `source.type=url` and `url`.
- Images returned by actual tools: Anthropic `tool_result` blocks and OpenAI `tool` messages are kept with their originating call identifiers and names.
- JPEG, PNG, GIF and WebP MIME types in base64 data URLs. External HTTP/HTTPS image URLs are forwarded as URLs, not fetched locally by emutools.

For example, an Anthropic image block:

```json
{
  "type": "image",
  "source": {
    "type": "base64",
    "media_type": "image/png",
    "data": "BASE64_IMAGE_BYTES"
  }
}
```

The placeholder must be replaced with actual base64 image data. Normalization produces an OpenAI `image_url` block. Inline encoded bytes and external URL values are preserved. Ordinary user captions and images retain their order.

## Out-of-order tool results

Two independent calls to the same tool may finish in either order. The proxy must not infer which result belongs to which call from its position.

Text-mode history now puts matching, quoted `history_id` labels on actual prior calls and results, including image-bearing results. These labels refer to existing history; they are not extra arguments for new calls. The original call arguments remain available so the model can identify the file or query associated with each result.

JSON mode already represents call/result IDs explicitly. With both `EMU_JSON_OUTPUT=true` and `EMU_IMAGE_INPUTS=true`, tool results contain JSON records with `id`, `name`, `is_error` and text image references. The real image blocks follow in the referenced order. Base64 bytes are not substituted with omission markers or embedded as ordinary result text.

This supplies correct association data. It does not guarantee that the model interprets it correctly; the live benchmark scores association and visual answers separately.

## Limits and privacy

- The existing 16 MiB inbound request cap includes base64 image data. Provider limits also apply.
- Inline data requires a supported MIME type, valid nonempty base64, and a valid image source shape. Malformed inputs return HTTP 400 before upstream I/O. Decoded image-file validity is checked by the provider, not by a local imaging library.
- Image detail accepts `auto`, `low`, and `high`. DeepSeek also documents `original`, but that value is not implemented by this proxy.
- Provider Files API references, documents, audio, Responses API, and system/assistant image input are not supported.
- Text truncation never cuts image bytes. For multimodal tool results it uses a shared text-prefix budget and a truncation marker; text-only tool results retain middle truncation.
- External URLs are sent to the upstream provider, which may download them. Prefer inline synthetic images for tests. Do not send confidential images to a provider without appropriate authorization.
- Request-body logging and raw benchmark captures can contain complete images and client transcripts. Keep them private. Normal body logging remains disabled by default.
- Token estimates are approximate and are not exact image pricing. The bounded DeepSeek test meter accounts for the documented maximum 384 input tokens per image in its reservations.

## Offline checks

```bash
python3 -m unittest discover -s tests -p 'test_image_inputs.py' -v
python3 -m unittest discover -s tests -p 'test_history_correlation.py' -v
python3 -m unittest discover -s tests -v
```

These test both incoming APIs, sync and SSE responses, default text mode and JSON mode, image-bearing tool results, result correlation, invalid inputs, and unchanged image bytes. Their local upstream is deterministic: they are not tests of model vision quality.

## Explicitly paid checks

These are separate scopes, not interchangeable evidence:

```bash
# Direct provider only; does not establish proxy or client compatibility.
python3 benchmarks/vision/probe.py

# Actual provider through emutools: HTTP fixtures plus actual Claude image reads.
# Maximum eight upstream requests and $0.02 peak-tariff bound.
python3 benchmarks/vision/proxy_probe.py --cli "$(command -v claude)" \
  --out-dir /tmp/emutools-vision-proxy

# Native Claude image reads only; no synthetic tool-result history.
# Maximum two upstream requests and $0.01 peak-tariff bound.
python3 benchmarks/vision/native_probe.py --cli "$(command -v claude)" \
  --out-dir /tmp/emutools-vision-native
```

Supply the key privately in `EMU_UPSTREAM_API_KEY`. Every output directory must be new. The native client receives dummy local authentication, is restricted to the image workspace, and has only Read available. Expected answers are not put in its prompt or workspace.

The probes retain exact OCR expectations even when transport works. They distinguish real reads, same-response batching, image-byte preservation, originating-call correlation, shape/color answers, and exact answers. A failed OCR check is not relabeled as an exact vision pass. Fragmented assistant trace events are grouped by their shared message identifier, and repeated blocks are deduplicated.

The capture bridge buffers upstream responses for metering; these probes do not establish first-token streaming latency. A socket timeout is not a guaranteed inference deadline. Unfinished requests remain fully reserved when the bounded client run ends, rather than being counted as free. See the [dated live report](vision-and-correlation-2026-09-07.md).

Provider references checked September 7, 2026:

- https://api-docs.deepseek.com/guides/vision
- https://api-docs.deepseek.com/quick_start/pricing/
