"""Bounded recovery without replaying already-delivered tool calls."""
import unittest
from unittest.mock import patch
import emutools.engine as engine
from emutools.core import Config, CanonMessage, CanonRequest, ToolDef, ToolCall
from emutools.protocol import render_tool_call_text


def chunks(text):
    for c in text:yield {'text':c}
    yield {'usage':{'prompt_tokens':11,'completion_tokens':7}}
    yield {'finish':'stop'}


def tool():return render_tool_call_text(ToolCall('Read',{'file_path':'actual.txt'}))


class RecoveryTests(unittest.TestCase):
    def request(self,**kw):
        return CanonRequest(model='test',messages=[CanonMessage('user','Read the file')],
                            tools=[ToolDef('Read',schema={'type':'object','properties':{'file_path':{'type':'string'}},'required':['file_path']})],stream=True,**kw)
    def recover(self,first,**kw):
        responses=iter([first,tool()])
        with patch.object(engine,'upstream_stream',side_effect=lambda *_:chunks(next(responses))) as upstream:
            events=list(engine.run_turn_stream(self.request(**kw),Config()))
        self.assertEqual(upstream.call_count,2)
        self.assertEqual(len([v for k,v in events if k=='call']),1)
        self.assertEqual([v for k,v in events if k=='usage'],[{'prompt_tokens':22,'completion_tokens':14}])
        self.assertEqual([v for k,v in events if k=='finish'],['tool_calls'])
        self.assertFalse(any(engine.EMPTY_FALLBACK in str(v) for k,v in events))
    def test_empty_stream_recovers(self):self.recover('')
    def test_whitespace_stream_recovers(self):self.recover(' \n ')
    def test_fabricated_result_stream_recovers(self):self.recover('<tool_result>invented</tool_result>not real')
    def test_truncated_tool_arguments_recover(self):self.recover('<tool_call>{"name":"Read","arguments":{"file_path":"half')
    def test_required_call_recovers_before_output(self):self.recover('',tool_choice='required')
    def test_empty_stream_fails_after_three_attempts(self):
        with patch.object(engine,'upstream_stream',side_effect=lambda *_:chunks('')) as upstream:
            with self.assertRaisesRegex(engine.UpstreamError,'no usable response'):
                list(engine.run_turn_stream(self.request(),Config()))
        self.assertEqual(upstream.call_count,3)
    def test_retry_setting_is_respected(self):
        with patch.object(engine,'upstream_stream',side_effect=lambda *_:chunks('')) as upstream:
            with self.assertRaises(engine.UpstreamError):list(engine.run_turn_stream(self.request(),Config(loop_retry=False)))
        self.assertEqual(upstream.call_count,1)
    def test_real_call_is_never_replayed(self):
        with patch.object(engine,'upstream_stream',side_effect=lambda *_:chunks(tool()+'<tool_result>invented</tool_result>')) as upstream:
            events=list(engine.run_turn_stream(self.request(),Config()))
        self.assertEqual(upstream.call_count,1);self.assertEqual(len([v for k,v in events if k=='call']),1)
    def test_visible_answer_is_never_replayed(self):
        with patch.object(engine,'upstream_stream',side_effect=lambda *_:chunks('Actual answer.')) as upstream:
            events=list(engine.run_turn_stream(self.request(),Config()))
        self.assertEqual(upstream.call_count,1);self.assertEqual(''.join(v for k,v in events if k=='text'),'Actual answer.')


    def test_unknown_tool_recovers(self):
        self.recover(render_tool_call_text(ToolCall('Unknown',{'file_path':'a'})))
    def test_invalid_schema_recovers(self):
        self.recover(render_tool_call_text(ToolCall('Read',{})))
    def test_rejected_call_after_prose_recovers(self):
        self.recover('Starting now.'+render_tool_call_text(ToolCall('Unknown',{})))
    def test_valid_call_before_rejected_call_is_not_replayed(self):
        raw=tool()+render_tool_call_text(ToolCall('Unknown',{}))
        with patch.object(engine,'upstream_stream',side_effect=lambda *_:chunks(raw)) as upstream:
            events=list(engine.run_turn_stream(self.request(),Config()))
        self.assertEqual(upstream.call_count,1)
        self.assertEqual(len([v for k,v in events if k=='call']),1)
    def test_rejected_calls_fail_after_bounded_repairs(self):
        raw=render_tool_call_text(ToolCall('Unknown',{}))
        with patch.object(engine,'upstream_stream',side_effect=lambda *_:chunks(raw)) as upstream:
            with self.assertRaises(engine.UpstreamError):list(engine.run_turn_stream(self.request(),Config()))
        self.assertEqual(upstream.call_count,3)
    def test_transport_failure_after_call_is_not_replayed(self):
        def broken(*args):
            for c in tool():yield {'text':c}
            raise engine.UpstreamError('connection lost',502)
        with patch.object(engine,'upstream_stream',side_effect=broken) as upstream:
            with self.assertRaises(engine.UpstreamError):list(engine.run_turn_stream(self.request(),Config()))
        self.assertEqual(upstream.call_count,1)


    def test_truncated_call_after_prose_recovers(self):
        self.recover('Starting now. <tool_call>{"name":"Read","arguments":{"file_path":"half')
    def test_unparseable_call_after_prose_recovers(self):
        self.recover('Starting now. <tool_call>not an actual call</tool_call>')
    def test_fabricated_result_after_prose_recovers(self):
        self.recover('Starting now. <tool_result>invented result</tool_result>')


if __name__=='__main__':unittest.main()
