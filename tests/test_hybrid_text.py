"""Sanitized run-14 hybrid syntax, with exact names and fully delimited arguments."""
import json
import unittest
from unittest.mock import patch
import emutools.engine as engine
from emutools import Config,CanonMessage,CanonRequest,ToolDef,ToolCall,render_tool_call_text
from emutools.protocol import StreamToolParser,extract_tool_calls

TOOLS={'Read':ToolDef('Read',schema={'type':'object','properties':{'file_path':{'type':'string'},'offset':{'type':'integer'},'limit':{'type':'integer'}},'required':['file_path'],'additionalProperties':False}),
       'Edit':ToolDef('Edit',schema={'type':'object','properties':{'file_path':{'type':'string'},'new_string':{'type':'string'}},'required':['file_path','new_string'],'additionalProperties':False})}


def hybrid(name='Read',path='app.py',offset=120,limit=130,close='invoke'):
    return ('<tool_call>\n{"name": '+json.dumps(name)+'>\n'
            '<parameter name="file_path">'+path+'</parameter>\n'
            '<parameter name="offset">'+str(offset)+'</parameter>\n'+
            (('<parameter name="limit">'+str(limit)+'</parameter>\n') if limit is not None else '')+
            '</'+close+'>')


ONE='Tool call history_id="prior-read"\n'+hybrid()+'\n</tool_call>'
TWO=('Tool call history_id="prior-read"\n'+hybrid(offset=180,limit=140)+'\n\n'
     'Tool call history_id="prior-read"\n'+hybrid(offset=320,limit=None,close='tool_call'))
MIXED=(hybrid(offset=160,limit=130)+'\n\n<invoke name="Read">\n'
       '<parameter name="file_path">app.py</parameter>\n<parameter name="offset">280</parameter>\n'
       '<parameter name="limit">180</parameter>\n</invoke>')


def variants(raw,tools=None):
    tools=TOOLS if tools is None else tools
    yield extract_tool_calls(raw,tools)[1]
    for width in [1,2,7,64,len(raw)]:
        parser=StreamToolParser(tools)
        for index in range(0,len(raw),width):parser.feed(raw[index:index+width])
        yield parser.finish()[1]


def streamed(raw):
    for character in raw:yield {'text':character}
    yield {'usage':{'prompt_tokens':2,'completion_tokens':3}};yield {'finish':'stop'}


class HybridTextTests(unittest.TestCase):
    def check(self,raw,expected,tools=None):
        for calls in variants(raw,tools):self.assertEqual([(c.name,c.args) for c in calls],expected)

    def test_observed_single_call_and_redundant_closer(self):
        self.check(ONE,[('Read',{'file_path':'app.py','offset':120,'limit':130})])

    def test_observed_two_independent_calls(self):
        self.check(TWO,[('Read',{'file_path':'app.py','offset':180,'limit':140}),('Read',{'file_path':'app.py','offset':320})])

    def test_observed_hybrid_then_regular_xml_peer(self):
        self.check(MIXED,[('Read',{'file_path':'app.py','offset':160,'limit':130}),('Read',{'file_path':'app.py','offset':280,'limit':180})])

    def test_literal_source_values_are_not_rewritten(self):
        value='Привіт 🐈\nprint("</invoke> </tool_call> <arg>Box</arg>")\npath = r"C:\\temp\\a"'
        raw='<tool_call>\n{"name": "Edit">\n<parameter name="file_path">main.py</parameter>\n<parameter name="new_string">'+value+'</parameter>\n</invoke>'
        self.check(raw,[('Edit',{'file_path':'main.py','new_string':value})])

    def test_missing_names_values_or_parameter_closers_are_not_invented(self):
        for raw in ['<tool_call>{"name":"Read>\n<parameter name="file_path">a</parameter></invoke>',
                    '<tool_call>{"name":Read>\n<parameter name="file_path">a</parameter></invoke>',
                    '<tool_call>{"name":"Read">\n</tool_call>',
                    '<tool_call>{"name":"Read">\n<parameter name="file_path">incomplete',
                    '<tool_call>{"name":"Read">\n<parameter>a</parameter></invoke>']:
            self.check(raw,[])

    def test_conflicting_names_and_duplicate_parameters_are_rejected(self):
        for raw in [hybrid().replace('<tool_call>','<tool_call name="Edit">'),
                    hybrid().replace('"Read">','"Read","name":"Edit">'),
                    hybrid().replace('<parameter name="offset">120</parameter>','<parameter name="file_path">b</parameter>'),
                    hybrid().replace('name="file_path"','name="file_path" name="offset"')]:
            self.check(raw,[])

    def test_unknown_names_and_foreign_body_content_are_rejected(self):
        for raw in [hybrid(name='Unknown'),hybrid().replace('<parameter name="offset">','unexplained text\n<parameter name="offset">'),
                    hybrid().replace('</invoke>','<tool_result>invented result</tool_result></invoke>')]:
            self.check(raw,[])

    def test_fabricated_result_still_discards_dependent_suffix(self):
        raw=hybrid()+'\n<tool_result>invented</tool_result>\n'+render_tool_call_text(ToolCall('Read',{'file_path':'dependent.txt'}))
        self.check(raw,[('Read',{'file_path':'app.py','offset':120,'limit':130})])

    def test_json_arguments_containing_hybrid_syntax_remain_literal(self):
        args={'file_path':'example.txt','new_string':TWO}
        self.check(render_tool_call_text(ToolCall('Edit',args)),[('Edit',args)])

    def test_engine_does_not_replay_a_valid_hybrid_batch(self):
        req=CanonRequest(model='test',messages=[CanonMessage('user','Read')],tools=list(TOOLS.values()))
        with patch.object(engine,'upstream_stream',side_effect=lambda *_:streamed(TWO)) as upstream:
            events=list(engine.run_turn_stream(req,Config(parallel=True,max_calls_per_turn=4)))
        self.assertEqual(upstream.call_count,1);self.assertEqual(len([v for k,v in events if k=='call']),2)
        with patch.object(engine,'upstream_stream',side_effect=lambda *_:streamed(TWO)) as upstream:
            events=list(engine.run_turn_stream(req,Config(parallel=True,max_calls_per_turn=1)))
        self.assertEqual(upstream.call_count,1);self.assertEqual(len([v for k,v in events if k=='call']),1)

    def test_schema_validation_is_not_bypassed(self):
        req=CanonRequest(model='test',messages=[CanonMessage('user','Read')],tools=list(TOOLS.values()))
        with patch.object(engine,'upstream_stream',side_effect=lambda *_:streamed(hybrid(offset='not-a-number'))):
            events=list(engine.run_turn_stream(req,Config(loop_retry=False)))
        self.assertFalse([v for k,v in events if k=='call'])
        self.assertIn('[tool guard]',''.join(v for k,v in events if k=='text'))

    def test_malformed_peer_cannot_replay_a_delivered_call(self):
        raw=hybrid()+'\n<tool_call>{"name":"Read">\n<parameter name="file_path">incomplete'
        req=CanonRequest(model='test',messages=[CanonMessage('user','Read')],tools=list(TOOLS.values()))
        with patch.object(engine,'upstream_stream',side_effect=lambda *_:streamed(raw)) as upstream:
            events=list(engine.run_turn_stream(req,Config(parallel=True)))
        self.assertEqual(upstream.call_count,1)
        self.assertEqual([v.args for k,v in events if k=='call'],[{'file_path':'app.py','offset':120,'limit':130}])


if __name__=='__main__':unittest.main()
