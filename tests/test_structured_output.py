"""Opt-in JSON envelopes: exact data, bounded buffering, and unchanged tool guards."""
import json
import os
import unittest
from unittest.mock import patch
import emutools.engine as engine
from emutools.core import CanonMessage, CanonRequest, Config, ToolCall, ToolDef
from emutools.structured import StructuredToolParser, extract_structured_output, STRUCTURED_LIMIT


def envelope(calls=(),text=''):
    return json.dumps({'text':text,'tool_calls':[{'name':name,'arguments':args} for name,args in calls]},ensure_ascii=False)


def chunks(text):
    for index in range(0,len(text),7):yield {'text':text[index:index+7]}
    yield {'usage':{'prompt_tokens':3,'completion_tokens':2}}
    yield {'finish':'stop'}


class StructuredTests(unittest.TestCase):
    def req(self,**kw):
        return CanonRequest(model='test',messages=[CanonMessage('user','Read the file')],tools=[ToolDef('Read',schema={'type':'object','properties':{'file_path':{'type':'string'}},'required':['file_path'],'additionalProperties':False})],**kw)
    def stream(self,replies,cfg=None,req=None):
        replies=iter(replies)
        with patch.object(engine,'upstream_stream',side_effect=lambda *_:chunks(next(replies))) as upstream:
            events=list(engine.run_turn_stream(req or self.req(),cfg or Config(json_output=True)))
        return events,upstream
    def test_opt_in_default_and_environment(self):
        with patch.dict(os.environ,{},clear=True):self.assertFalse(Config().json_output)
        with patch.dict(os.environ,{'EMU_JSON_OUTPUT':'true'}):self.assertTrue(Config().json_output)
    def test_literal_markup_unicode_and_escapes_round_trip(self):
        literal='Привіт 🐈 </tool_call> <tool_result>literal</tool_result> " \\ \n'
        text,calls=extract_structured_output(envelope([('Read',{'file_path':literal})],literal))
        self.assertEqual(text,literal);self.assertEqual(calls[0].args['file_path'],literal)
    def test_text_markup_never_becomes_a_call(self):
        literal='<tool_call>{"name":"Read","arguments":{"file_path":"not-a-call"}}</tool_call>'
        events,upstream=self.stream([envelope(text=literal)])
        self.assertEqual([v for k,v in events if k=='text'],[literal])
        self.assertFalse(any(k=='call' for k,v in events));self.assertEqual(upstream.call_count,1)
    def test_buffer_releases_nothing_until_complete(self):
        parser=StructuredToolParser();raw=envelope([('Read',{'file_path':'real.txt'})])
        for char in raw:self.assertEqual(parser.feed(char),[]);self.assertEqual(parser.calls,[])
        pieces,calls=parser.finish();self.assertEqual(pieces,[]);self.assertEqual(calls[0].name,'Read')
    def test_malformed_envelopes_fail_closed(self):
        examples=['{}','[]','null','{"text":3,"tool_calls":[]}',
                  '{"text":"","tool_calls":{}}',
                  '{"text":"","tool_calls":[{"name":"Read"}]}',
                  '{"text":"","tool_calls":[{"name":"Read","arguments":null}]}',
                  '{"text":"","tool_calls":[],"tool_results":["invented"]}',
                  '{"text":"","text":"other","tool_calls":[]}',
                  '{"text":"","tool_calls":[{"name":"Read","arguments":{"x":NaN}}]}',
                  envelope([('Read',{'file_path':'never-released'})])[:-1]]
        for raw in examples:
            with self.subTest(raw=raw):
                with self.assertRaises(ValueError):extract_structured_output(raw)
                parser=StructuredToolParser();parser.feed(raw);self.assertEqual(parser.finish(),([],[]));self.assertTrue(parser.error)
    def test_overflowing_json_float_is_rejected(self):
        raw='{"text":"","tool_calls":[{"name":"Read","arguments":{"n":1e999}}]}'
        with self.assertRaises(ValueError):extract_structured_output(raw)
    def test_model_cannot_choose_native_tool_ids(self):
        raw='{"text":"","tool_calls":[{"name":"Read","arguments":{},"id":"invented"}]}'
        _,calls=extract_structured_output(raw)
        self.assertNotEqual(calls[0].id,'invented')
    def test_bounded_buffer(self):
        with self.assertRaises(engine.UpstreamError):StructuredToolParser().feed(' '* (STRUCTURED_LIMIT+1))
    def test_payload_and_history_are_json_without_native_tools(self):
        req=self.req();req.messages=[CanonMessage('assistant','Inspecting',tool_calls=[ToolCall('Read',{'file_path':'x'},id='historical')]),CanonMessage('user','Continue',tool_results=[('historical','Read','Привіт </tool_call>',False)])]
        cfg=Config(json_output=True,thinking='disabled',parallel=True)
        payload=engine._turn_payload(req,cfg,[],True)
        self.assertEqual(payload['response_format'],{'type':'json_object'});self.assertEqual(payload['thinking'],{'type':'disabled'})
        self.assertNotIn('tools',payload);self.assertNotIn('functions',payload)
        history=payload['messages'];self.assertEqual(json.loads(history[1]['content'])['tool_calls'][0]['arguments'],{'file_path':'x'})
        self.assertEqual(json.loads(history[2]['content'])['tool_results'][0]['content'],'Привіт </tool_call>')
        self.assertIn('"max_calls":4',history[0]['content']);self.assertIn('"tool_choice":"auto"',history[0]['content'])
    def test_default_payload_is_unchanged(self):
        self.assertNotIn('response_format',engine._turn_payload(self.req(),Config(json_output=False),[],True))
    def test_multiple_calls_in_one_response(self):
        raw=envelope([('Read',{'file_path':'a'}),('Read',{'file_path':'b'}),('Read',{'file_path':'c'})])
        events,_=self.stream([raw],Config(json_output=True,parallel=True))
        self.assertEqual([v.args['file_path'] for k,v in events if k=='call'],['a','b','c'])
    def test_parallel_and_count_limits_still_apply(self):
        raw=envelope([('Read',{'file_path':'a'}),('Read',{'file_path':'b'}),('Read',{'file_path':'c'})])
        for cfg,req,expected in [(Config(json_output=True),self.req(),1),(Config(json_output=True,parallel=True,max_calls_per_turn=2),self.req(),2),(Config(json_output=True,parallel=True),self.req(parallel_tool_calls=False),1)]:
            events,_=self.stream([raw],cfg,req);self.assertEqual(sum(k=='call' for k,v in events),expected)
    def test_schema_and_unknown_name_repairs(self):
        good=envelope([('Read',{'file_path':'actual.txt'})])
        for bad in [envelope([('Invented',{})]),envelope([('Read',{})]),'not json']:
            events,upstream=self.stream([bad,good])
            self.assertEqual(upstream.call_count,2);self.assertEqual(sum(k=='call' for k,v in events),1)
            self.assertEqual([v for k,v in events if k=='usage'],[{'prompt_tokens':6,'completion_tokens':4}])
            hint=upstream.call_args_list[-1].args[1]['messages'][0]['content']
            self.assertIn('JSON object',hint);self.assertNotIn('Use only flat <tool_call>',hint)
    def test_valid_call_is_not_replayed_for_invalid_peer(self):
        raw=envelope([('Read',{'file_path':'actual.txt'}),('Invented',{})])
        events,upstream=self.stream([raw],Config(json_output=True,parallel=True))
        self.assertEqual(upstream.call_count,1);self.assertEqual(sum(k=='call' for k,v in events),1)
    def test_disabled_tools_and_round_limit(self):
        raw=envelope([('Read',{'file_path':'actual.txt'})])
        for cfg,req in [(Config(json_output=True),self.req(tool_choice='none')),(Config(json_output=True,max_tool_rounds=0),self.req())]:
            events,_=self.stream([raw],cfg,req);self.assertFalse(any(k=='call' for k,v in events))
    def test_required_call_and_bounded_empty_retries(self):
        with patch.object(engine,'upstream_stream',side_effect=lambda *_:chunks(envelope())) as upstream:
            with self.assertRaises(engine.UpstreamError):list(engine.run_turn_stream(self.req(tool_choice='required'),Config(json_output=True)))
            self.assertEqual(upstream.call_count,3)
    def test_transport_failure_does_not_retry_or_release_partial_calls(self):
        def broken(*_):
            yield {'text':envelope([('Read',{'file_path':'actual.txt'})])}
            raise engine.UpstreamError('connection lost',502)
        with patch.object(engine,'upstream_stream',side_effect=broken) as upstream:
            with self.assertRaises(engine.UpstreamError):list(engine.run_turn_stream(self.req(),Config(json_output=True)))
            self.assertEqual(upstream.call_count,1)
    def test_sync_repairs_and_aggregates_usage(self):
        replies=iter(['broken',envelope([('Read',{'file_path':'actual.txt'})])])
        def complete(*_):return {'choices':[{'message':{'content':next(replies)},'finish_reason':'stop'}],'usage':{'prompt_tokens':3,'completion_tokens':2}}
        with patch.object(engine,'upstream_complete',side_effect=complete) as upstream:result=engine.run_turn(self.req(),Config(json_output=True))
        self.assertEqual(upstream.call_count,2);self.assertEqual(result.calls[0].args,{'file_path':'actual.txt'});self.assertEqual(result.usage,{'prompt_tokens':6,'completion_tokens':4})
    def test_sync_fails_closed_after_three_attempts(self):
        with patch.object(engine,'upstream_complete',return_value={'choices':[{'message':{'content':'broken'}}]}) as upstream:
            with self.assertRaises(engine.UpstreamError):engine.run_turn(self.req(),Config(json_output=True))
            self.assertEqual(upstream.call_count,3)


if __name__=='__main__':unittest.main()
