"""Regressions for vendor channel markers and rejected-call recovery.

Both failures were found by driving Claude Code through emutools against a real
text-only DeepSeek model, not by reading the code:

* every assistant message carried visible `<｜｜DSML｜｜ calls>` garbage, because
  the wrapper stripper only knew the `<tool_calls>` spelling;
* the job stopped dead the first time the model named a tool the client had not
  registered, because the streaming path reported the rejection to the user as a
  successful assistant answer instead of re-asking the model.

Run: python3 -m unittest discover -s tests -v
No API key, third-party packages, or public internet required.
"""
import json
import unittest
from unittest.mock import patch

import emutools.engine as engine
from emutools.core import Config, CanonMessage, CanonRequest, ToolDef, ToolCall
from emutools.protocol import (
    StreamToolParser, extract_tool_calls, json_object_end, looks_like_botched_call,
    render_tool_call_text, render_tool_example, repair_args, strip_vendor_markup,
    validate_args,
)

PIPE = "\uff5c"
USEP = "\u2581"

TOOLS = [
    ToolDef("Read", schema={"type": "object", "required": ["file_path"],
                            "properties": {"file_path": {"type": "string"}}}),
    ToolDef("Write", schema={"type": "object", "required": ["file_path", "content"],
                             "properties": {"file_path": {"type": "string"},
                                            "content": {"type": "string"}}}),
]
BY_NAME = {t.name: t for t in TOOLS}
CALL = render_tool_call_text(ToolCall("Read", {"file_path": "a"}))
WIDTHS = (1, 2, 3, 5, 7, 13, 29, 97)


def stream_text_with(raw, tools, width):
    parser = StreamToolParser(tools)
    out = []
    for i in range(0, len(raw), width):
        out.extend(parser.feed(raw[i:i + width]))
    tail, _ = parser.finish()
    out.extend(tail)
    return "".join(out), parser.calls


def stream_text(raw, width):
    parser = StreamToolParser(BY_NAME)
    out = []
    for i in range(0, len(raw), width):
        out.extend(parser.feed(raw[i:i + width]))
    tail, _ = parser.finish()
    out.extend(tail)
    return "".join(out), parser.calls



def looping_history(times):
    """A transcript where the same call already ran `times` times."""
    messages = [CanonMessage("user", "go")]
    for i in range(times):
        messages.append(CanonMessage("assistant", "", tool_calls=[
            ToolCall("Read", {"file_path": "/a"}, id="toolu_%d" % i)]))
        messages.append(CanonMessage("user", "", tool_results=[
            ("toolu_%d" % i, "Read", "same", False)]))
    return messages


class VendorMarkerTests(unittest.TestCase):
    """A marker is syntax in the text channel and data inside an argument."""

    CASES = {
        # The exact shape observed live from DeepSeek through Claude Code.
        "dsml_spaced": "Hi.\n\n<%s%sDSML%s%s calls>\n\n%s\n</%s%sDSML%s%s calls>"
                       % (PIPE, PIPE, PIPE, PIPE, CALL, PIPE, PIPE, PIPE, PIPE),
        "dsml_tag": "Hi.\n<%s%sDSML%s%stool_calls>%s</%s%sDSML%s%stool_calls>"
                    % (PIPE, PIPE, PIPE, PIPE, CALL, PIPE, PIPE, PIPE, PIPE),
        "native_tag": "Hi.\n<%stool%scalls%sbegin%s>%s<%stool%scalls%send%s>"
                      % (PIPE, USEP, USEP, PIPE, CALL, PIPE, USEP, USEP, PIPE),
        "bracketless": "Hi.\n%stool%scalls%sbegin%s\n%s\n%stool%scalls%send%s"
                       % (PIPE, USEP, USEP, PIPE, CALL, PIPE, USEP, USEP, PIPE),
        "shared_pipes": "Hi.\n%stool%scalls%sbegin%s%stool%scall%sbegin%s%s%stool%scall%send%s"
                        % (PIPE, USEP, USEP, PIPE, PIPE, USEP, USEP, PIPE, CALL,
                           PIPE, USEP, USEP, PIPE),
        "classic_wrapper": "Hi.\n<tool_calls>%s</tool_calls>" % CALL,
    }

    def assert_clean(self, label, text):
        for junk in (PIPE, USEP, "DSML", "<"):
            self.assertNotIn(junk, text, "%s leaked %r: %r" % (label, junk, text))

    def test_markers_never_reach_the_text_channel(self):
        for name, raw in self.CASES.items():
            with self.subTest(case=name, mode="batch"):
                visible, calls = extract_tool_calls(raw, BY_NAME)
                self.assert_clean(name, visible)
                self.assertEqual([(c.name, c.args) for c in calls],
                                 [("Read", {"file_path": "a"})])
            for width in WIDTHS:
                with self.subTest(case=name, width=width):
                    visible, calls = stream_text(raw, width)
                    self.assert_clean(name, visible)
                    self.assertEqual([(c.name, c.args) for c in calls],
                                     [("Read", {"file_path": "a"})])

    def test_marker_text_survives_inside_an_argument(self):
        payload = "%s%sDSML%s%s %stool%scalls%sbegin%s %stool%scall%send%s" % (
            PIPE, PIPE, PIPE, PIPE, PIPE, USEP, USEP, PIPE, PIPE, USEP, USEP, PIPE)
        raw = "<tool_call>\n%s\n</tool_call>" % json.dumps(
            {"name": "Write", "arguments": {"file_path": "p.py", "content": payload}},
            ensure_ascii=False)
        _visible, calls = extract_tool_calls(raw, BY_NAME)
        self.assertEqual(calls[0].args["content"], payload)
        for width in WIDTHS:
            with self.subTest(width=width):
                _text, streamed_calls = stream_text(raw, width)
                self.assertEqual(streamed_calls[0].args["content"], payload)

    def test_markdown_pipes_are_not_vendor_markers(self):
        table = "| a | b |\n|---|---|\n| 1 | 2 |\n"
        self.assertEqual(strip_vendor_markup(table), table)
        for width in WIDTHS:
            with self.subTest(width=width):
                self.assertEqual(stream_text(table, width)[0], table)

    def test_prose_pipes_and_lone_markers_are_left_alone(self):
        for text in ("use a | b for alternatives", "cost is 5|10|15", "a||b"):
            with self.subTest(text=text):
                self.assertEqual(strip_vendor_markup(text), text)


def completion(text, finish="stop"):
    return {"choices": [{"message": {"content": text}, "finish_reason": finish}],
            "usage": {"prompt_tokens": 11, "completion_tokens": 7}}


def chunks(text, finish="stop"):
    for piece in text:
        yield {"text": piece}
    yield {"usage": {"prompt_tokens": 11, "completion_tokens": 7}}
    yield {"finish": finish}


class RejectedCallRecoveryTests(unittest.TestCase):
    """One unavailable tool name must not end the client's task."""

    BAD = ('I will create the file now.\n'
           '<tool_call>\n{"name":"Write2","arguments":{"file_path":"p","content":"x"}}\n'
           '</tool_call>')
    GOOD = render_tool_call_text(ToolCall("Write", {"file_path": "p", "content": "x"}))

    def request(self, **overrides):
        values = dict(model="deepseek-v4-pro", messages=[CanonMessage("user", "go")],
                      tools=TOOLS, stream=True)
        values.update(overrides)
        return CanonRequest(**values)

    def run_stream(self, replies, **overrides):
        seen = []
        remaining = list(replies)

        def fake_stream(_cfg, payload):
            seen.append(payload)
            return chunks(remaining.pop(0))

        with patch.object(engine, "upstream_stream", side_effect=fake_stream):
            events = list(engine.run_turn_stream(self.request(**overrides),
                                                 Config(use_stop=False)))
        return events, seen

    def test_unknown_tool_name_is_retried_and_recovered(self):
        events, payloads = self.run_stream([self.BAD, self.GOOD])
        calls = [v for k, v in events if k == "call"]
        text = "".join(v for k, v in events if k == "text")
        self.assertEqual([(c.name, c.args) for c in calls],
                         [("Write", {"file_path": "p", "content": "x"})])
        self.assertNotIn("[tool guard]", text)
        self.assertEqual(dict(events)["finish"] if False else
                         [v for k, v in events if k == "finish"], ["tool_calls"])
        self.assertEqual(len(payloads), 2, "the rejection must be re-asked upstream")
        retry_prompt = json.dumps(payloads[1], ensure_ascii=False)
        self.assertIn("was rejected", retry_prompt)
        self.assertIn("Write2", retry_prompt)

    def test_retry_prompt_names_the_available_tools_and_suggests_one(self):
        _events, payloads = self.run_stream([self.BAD, self.GOOD])
        retry_prompt = json.dumps(payloads[1], ensure_ascii=False)
        self.assertIn("Available tools", retry_prompt)
        self.assertIn("Did you mean `Write`?", retry_prompt)

    def test_invalid_arguments_are_retried_in_the_stream(self):
        bad_args = render_tool_call_text(ToolCall("Read", {"file_path": ["a"], "x": 1, "y": 2}))
        events, payloads = self.run_stream([bad_args, CALL])
        self.assertEqual([(c.name, c.args) for k, c in events if k == "call"],
                         [("Read", {"file_path": "a"})])
        self.assertEqual(len(payloads), 2)

    def test_retry_prompt_shows_the_correct_shape_for_that_tool(self):
        bad_args = render_tool_call_text(ToolCall("Read", {"file_path": ["a"], "x": 1, "y": 2}))
        _events, payloads = self.run_stream([bad_args, CALL])
        retry_prompt = json.dumps(payloads[1], ensure_ascii=False)
        self.assertIn('{\\"name\\": \\"Read\\", \\"arguments\\": {\\"file_path\\"', retry_prompt)

    def test_exhausted_retries_fail_retryably_instead_of_answering(self):
        # Three identical rejections must not become the assistant's answer: the
        # client would print the proxy's diagnostic and consider the task done.
        payloads = []

        def fake_stream(_cfg, payload):
            payloads.append(payload)
            return chunks(self.BAD)

        with patch.object(engine, "upstream_stream", side_effect=fake_stream):
            with self.assertRaises(engine.UpstreamError) as caught:
                list(engine.run_turn_stream(self.request(stream=True), Config(use_stop=False)))
        self.assertEqual(caught.exception.status, 529)
        self.assertIn("Write2", caught.exception.message)
        self.assertEqual(len(payloads), 3, "bounded: three attempts, not an endless loop")

    def test_retry_is_disabled_when_loop_retry_is_off(self):
        seen = []

        def fake_stream(_cfg, payload):
            seen.append(payload)
            return chunks(self.BAD)

        with patch.object(engine, "upstream_stream", side_effect=fake_stream):
            with self.assertRaises(engine.UpstreamError):
                list(engine.run_turn_stream(
                    self.request(), Config(use_stop=False, loop_retry=False)))
        self.assertEqual(len(seen), 1)

    def test_an_empty_reply_is_re_asked_not_forwarded(self):
        # Claude Code prints "the model returned an empty response" and stops, so an
        # empty sample has to be re-asked upstream inside the same client turn.
        payloads = []

        def fake_stream(_cfg, payload):
            payloads.append(payload)
            return chunks("" if len(payloads) < 3 else "done")

        with patch.object(engine, "upstream_stream", side_effect=fake_stream):
            events = list(engine.run_turn_stream(
                self.request(stream=True), Config(use_stop=False)))
        self.assertEqual("".join(v for k, v in events if k == "text"), "done")
        self.assertEqual(len(payloads), 3)
        self.assertIn("no text and no usable tool call",
                      json.dumps(payloads[1], ensure_ascii=False))

    def test_a_permanently_empty_reply_fails_retryably(self):
        with patch.object(engine, "upstream_stream", side_effect=lambda *_: chunks("")):
            with self.assertRaises(engine.UpstreamError) as caught:
                list(engine.run_turn_stream(self.request(stream=True), Config(use_stop=False)))
        self.assertEqual(caught.exception.status, 529)

    def test_prose_after_a_rejection_is_re_asked_not_accepted(self):
        # Live failure: after `Write` was rejected the model replied "Now I'll write
        # payload.py." with no call, and the client ended its task there.
        prose = "Now I'll write payload.py."
        events, payloads = self.run_stream([self.BAD, prose, self.GOOD])
        self.assertEqual([c.name for k, c in events if k == "call"], ["Write"])
        self.assertEqual(len(payloads), 3)
        self.assertIn("talked about the tool call instead of making it",
                      json.dumps(payloads[2], ensure_ascii=False))

    def test_prose_only_after_a_rejection_fails_retryably(self):
        with self.assertRaises(engine.UpstreamError) as caught:
            self.run_stream([self.BAD, "Now I'll write it.", "All done!"])
        self.assertEqual(caught.exception.status, 529)

    def test_a_plain_answer_with_no_rejection_is_still_an_answer(self):
        events, payloads = self.run_stream(["The file has 3 lines."])
        self.assertEqual(len(payloads), 1)
        self.assertEqual("".join(v for k, v in events if k == "text"),
                         "The file has 3 lines.")

    def test_a_repeated_call_is_re_asked_before_the_loop_guard_speaks(self):
        # Live failure: "[loop guard] Skipping a repeated `Read` call" was delivered as
        # the assistant's answer, so a stuck model ended the client's task.
        read = render_tool_call_text(ToolCall("Read", {"file_path": "/a"}))
        req = CanonRequest(model="m", messages=looping_history(3), tools=TOOLS, stream=True)

        seen = []

        def fake_stream(_cfg, payload):
            seen.append(payload)
            return chunks(read if len(seen) == 1 else "Nothing else to read.")

        with patch.object(engine, "upstream_stream", side_effect=fake_stream):
            events = list(engine.run_turn_stream(req, Config(use_stop=False, max_repeat=3)))
        text = "".join(v for k, v in events if k == "text")
        self.assertEqual(len(seen), 2)
        self.assertIn("Nothing else to read.", text)
        self.assertNotIn("loop guard", text)
        self.assertIn("will not change", json.dumps(seen[1], ensure_ascii=False))

    def test_a_truly_stuck_model_is_still_stopped_by_the_loop_guard(self):
        read = render_tool_call_text(ToolCall("Read", {"file_path": "/a"}))
        req = CanonRequest(model="m", messages=looping_history(3), tools=TOOLS, stream=True)
        with patch.object(engine, "upstream_stream", side_effect=lambda *_: chunks(read)):
            events = list(engine.run_turn_stream(req, Config(use_stop=False, max_repeat=3)))
        self.assertEqual([v for k, v in events if k == "call"], [])
        self.assertIn("loop guard", "".join(v for k, v in events if k == "text"))

    def test_unparseable_call_markup_is_re_asked(self):
        # Live failures: '<calls in parallel>' followed by loose '<arg name=...>', and
        # '<invoke name="Bash", "arguments": {...}' whose tag is never closed. Both
        # became the assistant's answer and ended the job.
        for botched in ('The fix:\n\n<arg name="file_path">/a</arg>',
                        '\n<invoke name="Bash", "arguments": {"command": "ls"}}\n</invoke>\n'):
            with self.subTest(botched=botched):
                events, payloads = self.run_stream([botched, self.GOOD])
                self.assertEqual([c.name for k, c in events if k == "call"], ["Write"])
                self.assertEqual(len(payloads), 2)
                self.assertIn("could not be parsed",
                              json.dumps(payloads[1], ensure_ascii=False))

    def test_ordinary_prose_is_not_mistaken_for_botched_markup(self):
        self.assertFalse(looks_like_botched_call("All three bugs are fixed. HARD_JOB_OK"))
        events, payloads = self.run_stream(["All three bugs are fixed."])
        self.assertEqual(len(payloads), 1)
        self.assertEqual("".join(v for k, v in events if k == "text"),
                         "All three bugs are fixed.")

    def test_a_wrapper_tag_with_trailing_words_is_still_a_wrapper(self):
        self.assertEqual(strip_vendor_markup("Fixing it:\n<calls in parallel>\ndone"),
                         "Fixing it:\n\ndone")

    def test_usage_accumulates_across_streaming_retries(self):
        events, _payloads = self.run_stream([self.BAD, self.GOOD])
        usage = [v for k, v in events if k == "usage"][-1]
        # Both attempts are billed upstream, so neither may be silently dropped.
        self.assertGreater(usage["prompt_tokens"], 11)
        self.assertGreater(usage["completion_tokens"], 7)

    def test_a_valid_call_is_never_retried(self):
        _events, payloads = self.run_stream([self.GOOD])
        self.assertEqual(len(payloads), 1)

    def test_named_tool_choice_still_fails_loudly(self):
        with self.assertRaises(engine.UpstreamError):
            self.run_stream([self.BAD], tool_choice="Read")

    def test_sync_path_suggests_the_closest_tool_too(self):
        req = CanonRequest(model="m", messages=[CanonMessage("user", "go")], tools=TOOLS)
        issues = engine._call_issues(ToolCall("Write2", {}), req, BY_NAME)
        self.assertIn("Did you mean `Write`?", issues[0])


class DoubledEnvelopeTests(unittest.TestCase):
    """A model that writes the call envelope twice still made an unambiguous call.

    Observed live: DeepSeek emitted
    `{"name":"Read","arguments":{"name":"Read","arguments":{"file_path":"..."}}}`
    and repeated it on every corrective retry, so rejecting it ended the task.
    """

    READ = ToolDef("Read", schema={"type": "object", "required": ["file_path"],
                                   "additionalProperties": False,
                                   "properties": {"file_path": {"type": "string"}}})
    BY_READ = {"Read": READ}

    CASES = {
        "arguments": '{"name":"Read","arguments":{"name":"Read","arguments":{"file_path":"/x"}}}',
        "input": '{"name":"Read","arguments":{"name":"Read","input":{"file_path":"/x"}}}',
        "parameters": '{"name":"Read","arguments":{"tool":"Read","parameters":{"file_path":"/x"}}}',
        "tripled": '{"name":"Read","arguments":{"name":"Read","arguments":'
                   '{"name":"Read","arguments":{"file_path":"/x"}}}}',
    }

    def test_doubled_envelope_is_unwrapped_and_valid(self):
        for name, body in self.CASES.items():
            raw = "<tool_call>\n%s\n</tool_call>" % body
            with self.subTest(case=name, mode="batch"):
                _visible, calls = extract_tool_calls(raw, self.BY_READ)
                self.assertEqual([(c.name, c.args) for c in calls],
                                 [("Read", {"file_path": "/x"})])
                self.assertEqual(validate_args(calls[0].args, self.READ.schema), [])
            with self.subTest(case=name, mode="stream"):
                _text, calls = stream_text_with(raw, self.BY_READ, 1)
                self.assertEqual([(c.name, c.args) for c in calls],
                                 [("Read", {"file_path": "/x"})])

    def test_plain_arguments_are_untouched(self):
        raw = '<tool_call>\n{"name":"Read","arguments":{"file_path":"/x"}}\n</tool_call>'
        _visible, calls = extract_tool_calls(raw, self.BY_READ)
        self.assertEqual(calls[0].args, {"file_path": "/x"})

    def test_a_tool_that_really_takes_name_and_input_is_not_rewritten(self):
        log = ToolDef("Log", schema={"type": "object", "required": ["name"],
                                     "properties": {"name": {"type": "string"},
                                                    "input": {"type": "object"}}})
        raw = '<tool_call>\n{"name":"Log","arguments":{"name":"Read","input":{"file_path":"/x"}}}\n</tool_call>'
        _visible, calls = extract_tool_calls(raw, {"Log": log})
        self.assertEqual(calls[0].args, {"name": "Read", "input": {"file_path": "/x"}})

    def test_unknown_extra_keys_block_the_unwrap(self):
        raw = ('<tool_call>\n{"name":"Read","arguments":'
               '{"name":"Read","arguments":{"file_path":"/x"},"stray":1}}\n</tool_call>')
        _visible, calls = extract_tool_calls(raw, self.BY_READ)
        self.assertNotEqual(calls[0].args, {"file_path": "/x"})


class ArgumentRepairTests(unittest.TestCase):
    """A nearly-right call is repaired instead of costing a whole retry round.

    Live runs showed deepseek-flash getting the call *shape* right and the
    parameter *names* wrong - `cmd` for Bash, a doubled `arguments` envelope, a
    stray key next to a missing required one - and then repeating the same mistake
    on every retry, which stalled the job.
    """

    BASH = {"type": "object", "required": ["command"],
            "properties": {"command": {"type": "string"},
                           "description": {"type": "string"},
                           "timeout": {"type": "number"}}}
    EDIT = {"type": "object", "required": ["file_path", "old_string", "new_string"],
            "properties": {"file_path": {"type": "string"},
                           "old_string": {"type": "string"},
                           "new_string": {"type": "string"},
                           "replace_all": {"type": "boolean"}}}

    def test_a_correct_call_is_left_alone(self):
        self.assertEqual(repair_args({"command": "ls"}, self.BASH), ({"command": "ls"}, False))

    def test_doubled_envelope_is_unwrapped(self):
        args = {"name": "Bash", "arguments": {"name": "Bash", "arguments": {"command": "ls"}}}
        self.assertEqual(repair_args(args, self.BASH)[0], {"command": "ls"})

    def test_envelope_holding_a_json_string_is_unwrapped(self):
        args = {"arguments": '{"command": "ls -la"}'}
        self.assertEqual(repair_args(args, self.BASH)[0], {"command": "ls -la"})

    def test_synonyms_are_renamed_to_the_schema_names(self):
        self.assertEqual(repair_args({"cmd": "ls"}, self.BASH)[0], {"command": "ls"})
        self.assertEqual(
            repair_args({"file_path": "/a", "old_str": "x", "new_str": "y"}, self.EDIT)[0],
            {"file_path": "/a", "old_string": "x", "new_string": "y"})

    def test_case_and_punctuation_differences_are_renamed(self):
        self.assertEqual(repair_args({"Command": "ls"}, self.BASH)[0], {"command": "ls"})
        self.assertEqual(repair_args({"file-path": "/a"}, {
            "type": "object", "required": ["file_path"],
            "properties": {"file_path": {"type": "string"}}})[0], {"file_path": "/a"})

    def test_a_rename_never_overwrites_a_value_already_present(self):
        args = {"command": "real", "cmd": "stray"}
        self.assertEqual(repair_args(args, self.BASH)[0], {"command": "real"})

    def test_one_stray_key_fills_the_one_missing_required_key(self):
        self.assertEqual(repair_args({"input_command": "ls"}, self.BASH)[0], {"command": "ls"})

    def test_two_strays_are_not_guessed_at(self):
        args = {"a": "ls", "b": "pwd"}
        self.assertEqual(repair_args(args, self.BASH)[0], {})

    def test_extra_keys_are_dropped_rather_than_failing_the_call(self):
        args = {"command": "ls", "reasoning": "because", "confidence": 0.9}
        self.assertEqual(repair_args(args, self.BASH)[0], {"command": "ls"})
        self.assertEqual(validate_args(repair_args(args, self.BASH)[0], self.BASH), [])

    def test_extra_keys_are_kept_when_the_schema_allows_them(self):
        schema = dict(self.BASH, additionalProperties=True)
        args = {"command": "ls", "extra": 1}
        self.assertEqual(repair_args(args, schema)[0], args)

    def test_repair_runs_end_to_end_through_the_parser(self):
        by_name = {"Bash": ToolDef("Bash", schema=self.BASH)}
        raw = '<tool_call>\n{"name":"Bash","arguments":{"cmd":"python3 test.py"}}\n</tool_call>'
        _visible, calls = extract_tool_calls(raw, by_name)
        self.assertEqual(calls[0].args, {"command": "python3 test.py"})
        self.assertTrue(calls[0].repaired)

    def test_rendered_example_matches_the_schema(self):
        example = json.loads(render_tool_example(ToolDef("Edit", schema=self.EDIT)))
        self.assertEqual(example["name"], "Edit")
        self.assertEqual(validate_args(example["arguments"], self.EDIT), [])


class BareCallAfterProseTests(unittest.TestCase):
    """Prose, and then a bare JSON call, is the commonest text-only-model shape.

    Observed live: deepseek-flash wrote "Now I'll fill in payload.py." and then a
    plain `{"name": "Edit", "arguments": {...}}` with no wrapper. The call leaked
    into the answer as text, the edit never happened, and the client ended its task
    believing it had succeeded.
    """

    TOOLS = {
        "Edit": ToolDef("Edit", schema={
            "type": "object", "required": ["file_path", "old_string", "new_string"],
            "properties": {"file_path": {"type": "string"},
                           "old_string": {"type": "string"},
                           "new_string": {"type": "string"}}}),
        "Read": ToolDef("Read", schema={"type": "object", "required": ["file_path"],
                                        "properties": {"file_path": {"type": "string"}}}),
    }
    RAW = ("Now I'll fill in payload.py.\n\n"
           '{"name": "Edit", "arguments": {"file_path": "/f/payload.py", '
           '"old_string": "PAYLOAD = \\"\\"", '
           '"new_string": "PAYLOAD = \\"</tool_call> {not json}\\""}}')

    def stream(self, raw, width):
        parser = StreamToolParser(self.TOOLS)
        pieces = []
        for i in range(0, len(raw), width):
            pieces.extend(parser.feed(raw[i:i + width]))
        tail, calls = parser.finish()
        return "".join(pieces + tail), calls

    def test_batch_splits_prose_from_the_call(self):
        visible, calls = extract_tool_calls(self.RAW, self.TOOLS)
        self.assertEqual(visible, "Now I'll fill in payload.py.")
        self.assertEqual([c.name for c in calls], ["Edit"])
        self.assertIn("</tool_call>", calls[0].args["new_string"])

    def test_stream_splits_prose_from_the_call_at_every_chunk_width(self):
        for width in (1, 2, 3, 5, 7, 13, 29, 97, 4096):
            with self.subTest(width=width):
                text, calls = self.stream(self.RAW, width)
                self.assertEqual([c.name for c in calls], ["Edit"])
                self.assertNotIn('{"name"', text)
                self.assertNotIn("</tool_call>", text)
                self.assertEqual(text.strip(), "Now I'll fill in payload.py.")

    def test_two_bare_calls_in_one_reply_are_both_found(self):
        raw = ('First.\n{"name": "Read", "arguments": {"file_path": "/a"}}\n'
               'Then.\n{"name": "Read", "arguments": {"file_path": "/b"}}\n')
        for width in (1, 11, 4096):
            with self.subTest(width=width):
                _text, calls = self.stream(raw, width)
                self.assertEqual([c.args["file_path"] for c in calls], ["/a", "/b"])

    def test_prose_with_an_unrelated_json_object_stays_text(self):
        raw = 'Here is a config:\n\n{"name": "not-a-tool", "value": 1}\n\nThat is all.'
        visible, calls = extract_tool_calls(raw, self.TOOLS)
        self.assertEqual(calls, [])
        self.assertEqual(visible, raw)
        text, calls = self.stream(raw, 1)
        self.assertEqual(calls, [])
        self.assertEqual(text, raw)

    def test_a_wrapped_call_still_wins_over_its_own_argument_block(self):
        raw = ('<tool_call>\n{"name": "Read", "arguments": {"file_path": "/a"}}\n'
               "</tool_call>")
        for width in (1, 7, 4096):
            with self.subTest(width=width):
                text, calls = self.stream(raw, width)
                self.assertEqual([(c.name, c.args) for c in calls],
                                 [("Read", {"file_path": "/a"})])
                self.assertEqual(text.strip(), "")

    def test_a_fenced_bare_call_loses_its_fence(self):
        raw = 'Doing it now.\n\n```json\n{"name": "Read", "arguments": {"file_path": "/a"}}\n```'
        for width in (1, 9, 4096):
            with self.subTest(width=width):
                text, calls = self.stream(raw, width)
                self.assertEqual([c.name for c in calls], ["Read"])
                self.assertNotIn("```json", text)

    def test_an_unfinished_bare_call_is_never_leaked_mid_stream(self):
        parser = StreamToolParser(self.TOOLS)
        emitted = parser.feed('Working.\n{"name": "Read", "arguments": {"file_p')
        self.assertEqual("".join(emitted).strip(), "Working.")
        emitted += parser.feed('ath": "/a"}}')
        tail, calls = parser.finish()
        self.assertEqual([c.args for c in calls], [{"file_path": "/a"}])
        self.assertNotIn('{"name"', "".join(emitted + tail))

    def test_a_bare_call_to_an_unregistered_tool_is_still_a_call(self):
        # Live failure: `{"name": "Write", ...}` was not a registered tool, so it was
        # left as prose, became the final answer, and the job stopped unfinished.
        raw = 'Let me rewrite the file.\n\n{"name": "Write", "arguments": {"file_path": "/a"}}'
        visible, calls = extract_tool_calls(raw, self.TOOLS)
        self.assertEqual([c.name for c in calls], ["Write"])
        self.assertEqual(visible, "Let me rewrite the file.")
        for width in (1, 13, 4096):
            with self.subTest(width=width):
                text, calls = self.stream(raw, width)
                self.assertEqual([c.name for c in calls], ["Write"])
                self.assertNotIn("Write", text)

    def test_an_openai_function_envelope_on_its_own_line_is_a_call(self):
        raw = ('Calling.\n{"type": "function", "function": {"name": "Read", '
               '"arguments": {"file_path": "/a"}}}')
        for width in (1, 13, 4096):
            with self.subTest(width=width):
                text, calls = self.stream(raw, width)
                self.assertEqual([(c.name, c.args) for c in calls],
                                 [("Read", {"file_path": "/a"})])
                self.assertEqual(text.strip(), "Calling.")

    def test_a_json_object_inside_a_sentence_is_not_a_call(self):
        for raw in ('The dict is {"name": "Read", "arguments": {"file_path": "/a"}} inline.',
                    'Schema:\n{"type": "object", "properties": {}}\nend'):
            with self.subTest(raw=raw):
                visible, calls = extract_tool_calls(raw, self.TOOLS)
                self.assertEqual(calls, [])
                self.assertEqual(visible, raw)
                text, calls = self.stream(raw, 1)
                self.assertEqual(calls, [])
                self.assertEqual(text, raw)

    def test_a_nameless_argument_object_is_matched_to_its_tool(self):
        # Live failure: the model wrote only the arguments, with no tool name at all,
        # and the whole object became the assistant's answer.
        raw = ('Let me inspect the bytes.\n\n'
               '{"file_path": "/a", "old_string": "x", "new_string": "y"}')
        visible, calls = extract_tool_calls(raw, self.TOOLS)
        self.assertEqual([(c.name, c.args) for c in calls],
                         [("Edit", {"file_path": "/a", "old_string": "x", "new_string": "y"})])
        self.assertEqual(visible, "Let me inspect the bytes.")
        for width in (1, 3, 17, 4096):
            with self.subTest(width=width):
                text, calls = self.stream(raw, width)
                self.assertEqual([c.name for c in calls], ["Edit"])
                self.assertNotIn("file_path", text)

    def test_a_nameless_object_that_fits_no_tool_stays_text(self):
        for raw in ('Note:\n{"offset": 5}\nend', 'Config:\n{"foo": 1, "bar": 2}\nend'):
            with self.subTest(raw=raw):
                visible, calls = extract_tool_calls(raw, self.TOOLS)
                self.assertEqual(calls, [])
                self.assertEqual(visible, raw)
                text, calls = self.stream(raw, 1)
                self.assertEqual(calls, [])
                self.assertEqual(text, raw)

    def test_a_nameless_object_that_fits_two_tools_stays_text(self):
        both = {"A": ToolDef("A", schema={"type": "object", "required": ["p"],
                                          "properties": {"p": {"type": "string"}}}),
                "B": ToolDef("B", schema={"type": "object", "required": ["p"],
                                          "properties": {"p": {"type": "string"}}})}
        raw = 'Doing it.\n{"p": "x"}'
        visible, calls = extract_tool_calls(raw, both)
        self.assertEqual(calls, [])
        self.assertEqual(visible, raw)

    def test_object_end_respects_strings_and_escapes(self):
        text = '{"a": "}\\\\", "b": {"c": "\\"}"}} tail'
        end = json_object_end(text, 0)
        self.assertEqual(text[end:], " tail")
        self.assertEqual(json_object_end('{"a": 1', 0), -1)


if __name__ == "__main__":
    unittest.main()
