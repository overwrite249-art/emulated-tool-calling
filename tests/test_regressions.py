"""Offline regressions for real-client protocol and safety failures.

Run: python3 -m unittest discover -s tests -v
No API key, third-party packages, or public internet required.
"""
import io
import json
import unittest
from unittest.mock import patch

import emutools.engine as engine
from emutools.core import Config, CanonMessage, CanonRequest, ToolDef, ToolCall
from emutools.protocol import (
    StreamToolParser, extract_tool_calls, render_tool_call_text, validate_args,
)
from emutools.wire import iter_sse, build_upstream_payload, openai_to_canon, anthropic_to_canon


TOOLS = [
    ToolDef("Read", schema={"type": "object", "required": ["file_path"],
                           "properties": {"file_path": {"type": "string"}}}),
    ToolDef("Write", schema={"type": "object", "required": ["file_path", "content"],
                            "properties": {"file_path": {"type": "string"}, "content": {"type": "string"}}}),
]
BY_NAME = {t.name: t for t in TOOLS}


def call(name="Read", **args):
    return render_tool_call_text(ToolCall(name, args))


def streamed(text, width=1):
    parser = StreamToolParser(BY_NAME)
    output = []
    for i in range(0, len(text), width):
        output.extend(parser.feed(text[i:i + width]))
    tail, calls = parser.finish()
    return "".join(output + tail).strip(), [(c.name, c.args) for c in calls]


def batch(text):
    visible, calls = extract_tool_calls(text, BY_NAME)
    return visible, [(c.name, c.args) for c in calls]


def completion(text, finish="stop"):
    return {"choices": [{"message": {"content": text}, "finish_reason": finish}],
            "usage": {"prompt_tokens": 11, "completion_tokens": 7}}


def chunks(text, finish="stop"):
    for c in text:
        yield {"text": c}
    yield {"usage": {"prompt_tokens": 11, "completion_tokens": 7}}
    yield {"finish": finish}


class FragmentedReader:
    """Read boundaries are bytes, not Unicode characters or SSE records."""
    def __init__(self, data, width):
        self.data = io.BytesIO(data)
        self.width = width

    def read(self, size=-1):
        return self.data.read(self.width if size < 0 else min(size, self.width))


class SSETests(unittest.TestCase):
    def test_utf8_split_at_every_byte_width(self):
        text = 'Привіт 🐈 <｜｜DSML｜｜invoke name="Read">'
        obj = {"choices": [{"delta": {"content": text}}]}
        data = ("data: " + json.dumps(obj, ensure_ascii=False) + "\n\ndata: [DONE]\n\n").encode()
        for width in range(1, 33):
            with self.subTest(width=width):
                self.assertEqual(list(iter_sse(FragmentedReader(data, width))), [obj])

    def test_multiline_data_and_crlf(self):
        data = b': ping\r\nevent: message\r\ndata: {"choices":\r\ndata: []}\r\n\r\ndata: [DONE]\r\n\r\n'
        self.assertEqual(list(iter_sse(FragmentedReader(data, 1))), [{"choices": []}])

    def test_read1_does_not_wait_for_a_full_kilobyte(self):
        class Immediate:
            data = [b'data: {"ok":true}\n\n', b'data: [DONE]\n\n']
            def read1(self, size):
                return self.data.pop(0) if self.data else b""
            def read(self, size):
                raise AssertionError("blocking read(size) used instead of read1(size)")
        self.assertEqual(next(iter_sse(Immediate())), {"ok": True})

    def test_final_frame_without_blank_line(self):
        self.assertEqual(list(iter_sse(io.BytesIO(b'data: {"tail":1}'))), [{"tail": 1}])


class ParserTests(unittest.TestCase):
    def assert_parser_parity(self, output, expected):
        self.assertEqual(batch(output), expected)
        for width in (1, 2, 3, 7, 17, 64, len(output)):
            with self.subTest(width=width):
                self.assertEqual(streamed(output, width), expected)

    def test_literal_close_tag_inside_json_is_preserved(self):
        args = {"file_path": "tags.txt", "content": 'literal </tool_call> and "quotes" \\ here'}
        self.assert_parser_parity(call("Write", **args), ("", [("Write", args)]))

    def test_literal_vendor_markup_in_json_is_preserved(self):
        args = {"file_path": "tags.txt", "content": '<tool_calls>keep</tool_calls> <｜｜DSML｜｜invoke>'}
        self.assert_parser_parity(call("Write", **args), ("", [("Write", args)]))

    def test_literal_parameter_markup_in_attribute_json_is_preserved(self):
        args = {"file_path": "tags.txt", "content": '<arg name="file_path">wrong</arg>'}
        raw = '<tool_call name="Write">' + json.dumps(args) + '</tool_call>'
        self.assert_parser_parity(raw, ("", [("Write", args)]))

    def test_hallucinated_result_discards_entire_suffix(self):
        output = 'Before. <tool_result>FABRICATED</tool_result> invented conclusion ' + call(file_path="fake")
        self.assert_parser_parity(output, ("Before.", []))

    def test_hallucinated_result_after_real_call_discards_suffix(self):
        output = call(file_path="real") + '<tool_result>FABRICATED</tool_result> invented conclusion ' + call(file_path="fake")
        self.assert_parser_parity(output, ("", [("Read", {"file_path": "real"})]))

    def test_result_like_text_inside_argument_is_not_discarded(self):
        args = {"file_path": "test.txt", "content": '<tool_result>literal</tool_result>'}
        self.assert_parser_parity(call("Write", **args), ("", [("Write", args)]))

    def test_bare_json_salvage_does_not_leak_json_before_call(self):
        args = {"file_path": "hello.txt"}
        raw = json.dumps({"name": "Read", "arguments": args})
        self.assert_parser_parity(raw, ("", [("Read", args)]))

    def test_non_call_json_is_still_visible(self):
        raw = '{"answer": "Привіт 🐈"}'
        self.assert_parser_parity(raw, (raw, []))

    def test_missing_close_tag_with_complete_json_is_safe(self):
        raw = call(file_path="read.txt").replace('</tool_call>', '')
        self.assert_parser_parity(raw, ("", [("Read", {"file_path": "read.txt"})]))

    def test_unterminated_string_is_not_invented(self):
        raw = '<tool_call>{"name":"Write","arguments":{"file_path":"x","content":"half'
        self.assertFalse(batch(raw)[1])
        self.assertFalse(streamed(raw)[1])


class PolicyTests(unittest.TestCase):
    def request(self, **overrides):
        values = dict(model="deepseek-v4-pro", messages=[CanonMessage("user", "test")], tools=TOOLS)
        values.update(overrides)
        return CanonRequest(**values)

    def run_sync(self, text, **overrides):
        req = self.request(**overrides)
        with patch.object(engine, "upstream_complete", return_value=completion(text)):
            return engine.run_turn(req, Config(loop_retry=False, use_stop=False))

    def run_stream(self, text, **overrides):
        req = self.request(stream=True, **overrides)
        with patch.object(engine, "upstream_stream", side_effect=lambda *_: chunks(text)):
            return list(engine.run_turn_stream(req, Config(loop_retry=False, use_stop=False)))

    def test_none_blocks_sync_calls_even_when_model_ignores_prompt(self):
        self.assertEqual(self.run_sync(call(file_path="a"), tool_choice="none").calls, [])

    def test_none_blocks_stream_calls_even_when_model_ignores_prompt(self):
        self.assertFalse([v for k, v in self.run_stream(call(file_path="a"), tool_choice="none") if k == "call"])

    def test_named_choice_blocks_different_sync_tool(self):
        with self.assertRaises(engine.UpstreamError):
            self.run_sync(call(file_path="a"), tool_choice="Write")

    def test_named_choice_blocks_different_stream_tool(self):
        with self.assertRaises(engine.UpstreamError):
            self.run_stream(call(file_path="a"), tool_choice="Write")

    def test_invalid_sync_arguments_never_reach_client(self):
        result = self.run_sync(call())
        self.assertFalse(result.calls)
        self.assertTrue(result.text.strip())

    def test_invalid_stream_arguments_never_reach_client(self):
        events = self.run_stream(call())
        self.assertFalse([v for k, v in events if k == "call"])
        self.assertTrue([v for k, v in events if k == "text"])

    def test_sync_validation_fails_closed_after_all_repairs(self):
        with patch.object(engine, "upstream_complete", return_value=completion(call())) as upstream:
            result = engine.run_turn(self.request(), Config(use_stop=False))
        self.assertEqual(upstream.call_count, 3)
        self.assertFalse(result.calls)
        self.assertTrue(result.text.strip())
        self.assertEqual(result.usage.get("prompt_tokens"), 33)
        self.assertEqual(result.usage.get("completion_tokens"), 21)

    def test_parallel_false_is_enforced_sync(self):
        result = self.run_sync(call(file_path="a") + call(file_path="b"))
        self.assertEqual(len(result.calls), 1)

    def test_parallel_false_is_enforced_stream(self):
        events = self.run_stream(call(file_path="a") + call(file_path="b"))
        self.assertEqual(len([v for k, v in events if k == "call"]), 1)

    def test_client_parallel_false_is_not_overridden(self):
        body = {"model": "x", "messages": [{"role": "user", "content": "x"}], "parallel_tool_calls": False}
        req = openai_to_canon(body, Config(parallel=True))
        self.assertIs(req.parallel_tool_calls, False)
        body = {"model": "x", "messages": [{"role": "user", "content": "x"}],
                "tool_choice": {"type": "auto", "disable_parallel_tool_use": True}}
        req = anthropic_to_canon(body, Config(parallel=True))
        self.assertIs(req.parallel_tool_calls, False)

    def test_default_stop_does_not_truncate_source_code(self):
        req = self.request()
        self.assertNotIn("</tool_call>", build_upstream_payload(req, Config(), [], True).get("stop", []))

    def test_stream_errors_are_not_successful_assistant_messages(self):
        req = self.request(stream=True)
        def fail(*_):
            raise engine.UpstreamError("upstream unavailable", 503)
            yield
        with patch.object(engine, "run_turn_stream", side_effect=fail):
            anthropic = b"".join(engine.anthropic_stream_bytes(req, Config()))
            openai = b"".join(engine.openai_stream_bytes(req, Config(), False))
        self.assertIn(b'event: error', anthropic)
        self.assertNotIn(b'event: message_stop', anthropic)
        self.assertIn(b'"error":', openai)
        self.assertNotIn(b'"finish_reason": "stop"', openai)

    def test_anthropic_stream_reports_input_tokens(self):
        req = self.request(stream=True)
        with patch.object(engine, "run_turn_stream", return_value=iter([
            ("text", "ok"), ("usage", {"prompt_tokens": 123, "completion_tokens": 4}), ("finish", "stop")
        ])):
            events = list(iter_sse(io.BytesIO(b"".join(engine.anthropic_stream_bytes(req, Config())))))
        delta = next(x for x in events if x.get("type") == "message_delta")
        self.assertEqual(delta["usage"].get("input_tokens"), 123)


class ConfigAndSchemaTests(unittest.TestCase):
    def test_documented_model_map_syntax(self):
        cfg = Config(model_map_raw="my-model=deepseek-v4-pro,tiny=deepseek-v4-flash")
        self.assertEqual(cfg.model_map(), {"my-model": "deepseek-v4-pro", "tiny": "deepseek-v4-flash"})

    def test_nested_required_is_validated(self):
        schema = {"properties": {"input": {"type": "object", "required": ["path"]}}}
        self.assertTrue(validate_args({"input": {}}, schema))

    def test_array_item_type_is_validated(self):
        schema = {"properties": {"files": {"type": "array", "items": {"type": "string"}}}}
        self.assertTrue(validate_args({"files": [12]}, schema))

    def test_additional_properties_false_is_validated(self):
        schema = {"properties": {"path": {"type": "string"}}, "additionalProperties": False}
        self.assertTrue(validate_args({"path": "x", "unknown": "y"}, schema))

    def test_nullable_union_accepts_null(self):
        schema = {"properties": {"n": {"type": ["null", "integer"]}}}
        self.assertEqual(validate_args({"n": None}, schema), [])


if __name__ == "__main__":
    unittest.main()
