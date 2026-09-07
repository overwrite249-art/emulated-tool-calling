"""V4 occasionally wraps valid tool calls in anonymous tool_call containers."""
import json
import unittest
from emutools.core import ToolDef, ToolCall
from emutools.protocol import StreamToolParser, extract_tool_calls, render_tool_call_text

TOOLS={t.name:t for t in [ToolDef('Read'),ToolDef('Edit')]}

def call(name='Read',**args):return render_tool_call_text(ToolCall(name,args))

def both(raw):
    text,calls=extract_tool_calls(raw,TOOLS)
    yield text,[(c.name,c.args) for c in calls]
    for width in (1,2,7,64,len(raw)):
        p=StreamToolParser(TOOLS);out=[]
        for i in range(0,len(raw),width):out.extend(p.feed(raw[i:i+width]))
        tail,calls=p.finish();yield ''.join(out+tail).strip(),[(c.name,c.args) for c in calls]


class NestedCallsTests(unittest.TestCase):
    def check(self,raw,calls,text=''):
        for value in both(raw):self.assertEqual(value,(text,calls))
    def test_anonymous_wrapper_around_one_call(self):
        self.check('<tool_call>'+call(file_path='a')+'</tool_call>',[('Read',{'file_path':'a'})])
    def test_wrapper_with_preface_still_executes_call(self):
        self.check('Before.<tool_call>'+call(file_path='a')+'</tool_call>After.',[('Read',{'file_path':'a'})],'Before.After.')
    def test_multiple_calls_inside_one_wrapper(self):
        self.check('<tool_call>'+call(file_path='a')+call(file_path='b')+'</tool_call>',[('Read',{'file_path':'a'}),('Read',{'file_path':'b'})])
    def test_multiple_wrapper_levels(self):
        self.check('<tool_call>'*3+call(file_path='a')+'</tool_call>'*3,[('Read',{'file_path':'a'})])
    def test_json_argument_tags_are_literal(self):
        args={'file_path':'a','old_string':'','new_string':'print("<tool_call><tool_call name=Read>literal</tool_call></tool_call>")'}
        self.check('<tool_call>'+call('Edit',**args)+'</tool_call>',[('Edit',args)])
    def test_json_anonymous_call_is_not_a_wrapper(self):
        self.check(call(file_path='a'),[('Read',{'file_path':'a'})])
    def test_fabricated_result_still_discards_suffix(self):
        self.check('<tool_call>'+call(file_path='a')+'<tool_result>fake</tool_result>'+call(file_path='b')+'</tool_call>',[('Read',{'file_path':'a'})])
    def test_unterminated_inner_string_never_executes(self):
        self.check('<tool_call><tool_call>{"name":"Read","arguments":{"file_path":"half',[])
    def test_closing_wrapper_whitespace(self):
        self.check('<tool_call>'+call(file_path='a')+'</tool_call    >',[('Read',{'file_path':'a'})])


if __name__=='__main__':unittest.main()
