# --- generated header: build_single_file.py strips these blocks ---
from __future__ import annotations
from ._prelude import *  # noqa: F401,F403
from .core import *  # noqa: F401,F403
from .protocol import render_tool_result_text
# --- end generated header ---

import base64
import urllib.parse


class ImageInputError(ValueError):
    """Invalid opt-in image input; no URL is fetched by this module."""


_IMAGE_TYPES = ('image/jpeg', 'image/png', 'image/gif', 'image/webp')


def _image_url(block):
    detail = None
    if block.get('type') == 'image_url':
        value = block.get('image_url')
        if isinstance(value, dict):
            url, detail = value.get('url'), value.get('detail')
        else:
            url = value
    else:
        source = block.get('source')
        if not isinstance(source, dict):
            raise ImageInputError('image.source must be an object')
        if source.get('type') == 'base64':
            media_type, data = source.get('media_type'), source.get('data')
            if media_type not in _IMAGE_TYPES or not isinstance(data, str):
                raise ImageInputError('base64 images require a supported media_type and data string')
            url = 'data:' + media_type + ';base64,' + data
        elif source.get('type') == 'url':
            url = source.get('url')
        else:
            raise ImageInputError('image.source.type must be base64 or url')
    if not isinstance(url, str) or not url or any(ord(c) < 32 for c in url):
        raise ImageInputError('image URL must be a non-empty string without control characters')
    if url.startswith('data:'):
        header, comma, data = url.partition(',')
        if not comma or header not in ('data:' + mime + ';base64' for mime in _IMAGE_TYPES):
            raise ImageInputError('image data URLs must use supported base64 image media types')
        try:
            decoded = base64.b64decode(data, validate=True)
        except (ValueError, TypeError) as exc:
            raise ImageInputError('image data is not valid base64') from exc
        if not decoded:
            raise ImageInputError('image data must not be empty')
    else:
        try:
            parsed = urllib.parse.urlsplit(url)
            valid = parsed.scheme in ('http', 'https') and bool(parsed.hostname)
        except ValueError:
            valid = False
        if not valid:
            raise ImageInputError('image URLs must use http, https, or a base64 image data URL')
    image = {'url': url}
    if detail is not None:
        if detail not in ('auto', 'low', 'high'):
            raise ImageInputError('image detail must be auto, low, or high')
        image['detail'] = detail
    return {'type': 'image_url', 'image_url': image}


def image_history_marker(block):
    # Distinct image observations must not be mistaken for identical text results.
    image = _image_url(block)
    digest = hashlib.sha256(canon_json(image).encode('utf-8')).hexdigest()
    return '[image attached; sha256:' + digest + ']'


def _normalize_image_parts(content, names, depth=0):
    if depth > 1:
        raise ImageInputError('nested tool_result image content is not supported')
    values = content if isinstance(content, list) else [content]
    parts = []
    for block in values:
        if block is None:
            continue
        if isinstance(block, str):
            parts.append({'type': 'text', 'text': block})
            continue
        if not isinstance(block, dict):
            parts.append({'type': 'text', 'text': safe_str(block)})
            continue
        kind = block.get('type')
        if kind in ('image', 'image_url'):
            parts.append(_image_url(block))
        elif kind == 'tool_result':
            tid = safe_str(block.get('tool_use_id'))
            parts.append({'type': 'tool_result', 'id': tid, 'name': names.get(tid, 'tool'),
                          'is_error': bool(block.get('is_error')),
                          'content': _normalize_image_parts(block.get('content'), names, depth + 1)})
        elif kind in ('thinking', 'redacted_thinking', 'tool_use'):
            continue
        elif kind == 'document':
            parts.append({'type': 'text', 'text': '[document omitted: this model is text-only]'})
        elif kind in ('audio', 'input_audio'):
            parts.append({'type': 'text', 'text': '[audio omitted: this model is text-only]'})
        elif kind == 'text' or 'text' in block:
            parts.append({'type': 'text', 'text': safe_str(block.get('text'))})
    return parts


def _has_image(parts):
    return any(part['type'] == 'image_url' or
               (part['type'] == 'tool_result' and _has_image(part['content'])) for part in parts)


def normalize_image_content(content, names=None):
    parts = _normalize_image_parts(content, names or {})
    return parts if _has_image(parts) else []


def _limit_image_result_text(parts, limit):
    if limit <= 0:
        return parts
    remaining, warned, result = limit, False, []
    for part in parts:
        if part['type'] == 'image_url':
            result.append(part)
            continue
        text = part['text']
        kept = text[:remaining]
        remaining -= len(kept)
        if kept:
            result.append({'type': 'text', 'text': kept})
        if len(kept) < len(text) and not warned:
            result.append({'type': 'text', 'text': '\n[tool-result text truncated]\n'})
            warned = True
    return result


def render_image_content(parts, cfg, structured=False):
    """Preserve user captions/image order and correlate image-bearing tool results."""
    result = []
    for part in parts:
        kind = part['type']
        if kind == 'text':
            result.append({'type': 'text', 'text': part['text']})
        elif kind == 'image_url':
            result.append({'type': 'image_url', 'image_url': dict(part['image_url'])})
        elif kind == 'tool_result':
            inner = _limit_image_result_text(render_image_content(part['content'], cfg), cfg.max_result_chars)
            if structured:
                images, text = [], []
                for child in inner:
                    if child['type'] == 'image_url':
                        images.append(child)
                        text.append('[image %d attached below]' % len(images))
                    else:
                        text.append(child['text'])
                record = {'tool_results': [{'id': part['id'], 'name': part['name'],
                          'content': '\n'.join(text), 'is_error': part['is_error']}]}
                result.append({'type': 'text', 'text': json.dumps(record, ensure_ascii=False, separators=(',', ':'))})
                result.extend(images)
            else:
                envelope = render_tool_result_text(part['name'], '', part['is_error'], 0)
                prefix, _, suffix = envelope.partition('\n')
                result.append({'type': 'text', 'text': prefix + '\n'})
                result.extend(inner)
                result.append({'type': 'text', 'text': suffix})
        else:
            raise ImageInputError('unsupported canonical image content part')
    return result


def merge_message_content(left, right):
    if isinstance(left, str) and isinstance(right, str):
        return left + '\n\n' + right
    def parts(value):
        return list(value) if isinstance(value, list) else [{'type': 'text', 'text': value}]
    return parts(left) + [{'type': 'text', 'text': '\n\n'}] + parts(right)


# --- generated header: build_single_file.py strips these blocks ---
__all__ = ['ImageInputError', 'image_history_marker', 'normalize_image_content',
           'render_image_content', 'merge_message_content']
# --- end generated header ---
