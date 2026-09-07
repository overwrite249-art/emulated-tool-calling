"""Ambiguous model syntax must not silently select a tool or discard an argument."""
import json
import unittest
from unittest.mock import patch
import emutools.engine as engine
from emutools import Config,CanonMessage,CanonRequest,ToolDef,ToolCall,render_tool_call_text
from emutools.protocol import StreamToolParser,extract_tool_calls

TOOLS={name:ToolDef(name,schema={'type':'object','properties':{'file_path':{'type':'string'}},'required':['file_path'],'additionalProperties':False}) for name in ['Read','Inspect']}


def variants(raw,tools=None):
    tools=TOOLS if tools is None else tools
    yield extract_tool_calls(raw,tools)[1]
    for width in [1,2,7,64,len(raw)]:
        parser=StreamToolParser(tools)
        for index in range(0,len(raw),width):parser.feed(raw[index:index+width])
        yield parser.finish()[1]


def wrapped(body,attrs=''):
    return '<tool_call'+attrs+'>'+body+'</tool_call>'


class AmbiguousCallTests(unittest.TestCase):
    def reject(self,raw):
        for calls in variants(raw):self.assertEqual(calls,[])

    def test_observed_wrapped_and_flat_paths_are_not_silently_selected(self):
        # Sanitized run-13 Read: the old decoder selected build.py and dropped web/app.ts.
        self.reject(wrapped('{"arguments":{"file_path":"build.py"},"file_path":"web/app.ts"}',' name="Read"'))

    def test_conflicting_outer_and_inner_names_are_rejected(self):
        self.reject(wrapped('{"name":"Inspect","arguments":{"file_path":"a"}}',' name="Read"'))

    def test_conflicting_name_aliases_are_rejected(self):
        self.reject(wrapped('{"name":"Read","tool":"Inspect","arguments":{"file_path":"a"}}'))
        self.reject(wrapped('{"file_path":"a"}',' name="Read" tool="Inspect"'))

    def test_conflicting_argument_aliases_are_rejected(self):
        self.reject(wrapped('{"name":"Read","arguments":{"file_path":"a"},"input":{"file_path":"b"}}'))

    def test_conflicting_nested_function_names_and_arguments_are_rejected(self):
        self.reject(wrapped('{"name":"Read","arguments":{"file_path":"a"},"function":{"name":"Inspect","arguments":{"file_path":"b"}}}'))
        self.reject(wrapped('{"name":"Read","arguments":{"file_path":"a"},"function":{"name":"Read","arguments":{"file_path":"b"}}}'))

    def test_duplicate_json_names_and_nested_arguments_are_rejected(self):
        self.reject(wrapped('{"name":"Read","name":"Inspect","arguments":{"file_path":"a"}}'))
        self.reject(wrapped('{"name":"Read","arguments":{"file_path":"a","file_path":"b"}}'))

    def test_duplicate_keys_cannot_escape_through_repair_or_python_literals(self):
        self.reject(wrapped('{"name":"Read","arguments":{"file_path":"a","file_path":"b",},}'))
        self.reject(wrapped("{'name':'Read','arguments':{'file_path':'a','file_path':'b'}}"))

    def test_duplicate_xml_attributes_are_rejected(self):
        self.reject(wrapped('{"file_path":"a"}',' name="Read" name="Inspect"'))
        self.reject(wrapped('<arg name="file_path" name="different">a</arg>',' name="Read"'))

    def test_duplicate_xml_parameters_are_rejected(self):
        self.reject(wrapped('<arg name="file_path">a</arg><arg name="file_path">b</arg>',' name="Read"'))

    def test_nonfinite_literals_are_not_repaired_into_calls(self):
        for number in ['NaN','Infinity','-Infinity','1e999']:
            self.reject(wrapped('{"name":"Read","arguments":{"file_path":'+number+'}}'))

    def test_unambiguous_supported_envelopes_still_work(self):
        examples=[wrapped('{"name":"Read","arguments":{"file_path":"a"}}'),
                  wrapped('{"file_path":"a"}',' name="Read"'),
                  wrapped('{"arguments":{"file_path":"a"}}',' name="Read"'),
                  wrapped('{"name":"Read","file_path":"a"}'),
                  wrapped('{"type":"function","function":{"name":"Read","arguments":"{\\"file_path\\":\\"a\\"}"}}'),
                  wrapped("{'name':'Read','arguments':{'file_path':'a'}}")]
        for raw in examples:
            for calls in variants(raw):self.assertEqual([(c.name,c.args) for c in calls],[('Read',{'file_path':'a'})])

    def test_identical_redundant_aliases_are_not_conflicts(self):
        raw=wrapped('{"name":"Read","tool":"Read","arguments":{"file_path":"a"},"input":{"file_path":"a"}}',' name="Read"')
        for calls in variants(raw):self.assertEqual([(c.name,c.args) for c in calls],[('Read',{'file_path':'a'})])

    def test_reserved_words_and_duplicate_looking_source_inside_arguments_are_data(self):
        args={'name':'ordinary data','arguments':{'file_path':'inner'},'file_path':'outer',
              'source':'{"file_path":"a","file_path":"b"}\n<arg name="x">a</arg><arg name="x">b</arg>\nПривіт </tool_call>'}
        raw=render_tool_call_text(ToolCall('Echo',args));tools={'Echo':ToolDef('Echo')}
        for calls in variants(raw,tools):self.assertEqual([(c.name,c.args) for c in calls],[('Echo',args)])

    def test_colliding_normalized_names_are_not_arbitrarily_resolved(self):
        for names,requested in [(['Read-File','Read.File'],'readfile'),(['Read','read'],'READ')]:
            tools={name:ToolDef(name) for name in names};raw=render_tool_call_text(ToolCall(requested,{}))
            for calls in variants(raw,tools):self.assertFalse(any(c.name in tools for c in calls))
            def stream(*_):
                yield {'text':raw};yield {'usage':{'prompt_tokens':1,'completion_tokens':1}};yield {'finish':'stop'}
            req=CanonRequest(model='test',messages=[CanonMessage('user','Read')],tools=list(tools.values()))
            with patch.object(engine,'upstream_stream',side_effect=stream):events=list(engine.run_turn_stream(req,Config(loop_retry=False)))
            self.assertFalse([v for k,v in events if k=='call'])

    def test_exact_and_unique_normalized_names_remain_supported(self):
        for tools,requested,expected in [({'Read-File':ToolDef('Read-File'),'Read.File':ToolDef('Read.File')},'Read-File','Read-File'),
                                         ({'Read-File':ToolDef('Read-File')},'readfile','Read-File')]:
            raw=render_tool_call_text(ToolCall(requested,{}))
            for calls in variants(raw,tools):self.assertEqual([c.name for c in calls],[expected])


if __name__=='__main__':unittest.main()
