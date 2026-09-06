"""Real V4 output can wrap complete named JSON calls in bare DSML tags."""
import json
import unittest
from emutools import ToolDef
from emutools.protocol import StreamToolParser,extract_tool_calls

TOOLS={name:ToolDef(name) for name in ('Read','Edit')}
MARKERS=('｜｜DSML｜｜','||DSML||','｜DSML｜','|DSML|')


def wrap(name,args,marker=MARKERS[0]):
    return '<'+marker+'>\n'+json.dumps({'name':name,'arguments':args},ensure_ascii=False)+'\n</'+marker+'>'


def variants(raw):
    text,calls=extract_tool_calls(raw,TOOLS)
    yield text,[(c.name,c.args) for c in calls]
    for width in (1,2,7,64,len(raw)):
        parser=StreamToolParser(TOOLS);text=[]
        for start in range(0,len(raw),width):text.extend(parser.feed(raw[start:start+width]))
        tail,calls=parser.finish()
        yield ''.join(text+tail).strip(),[(c.name,c.args) for c in calls]


class BareDSMLTests(unittest.TestCase):
    def test_observed_prefaced_two_call_response(self):
        raw='Inspecting.\n\n'+wrap('Read',{'file_path':'app.py'})+'\n'+wrap('Read',{'file_path':'build.py'})
        expected=('Inspecting.',[('Read',{'file_path':'app.py'}),('Read',{'file_path':'build.py'})])
        for result in variants(raw):self.assertEqual(result,expected)

    def test_ascii_and_fullwidth_boundaries(self):
        for marker in MARKERS:
            raw=wrap('Read',{'file_path':'Привіт.py'},marker)
            for result in variants(raw):self.assertEqual(result,('',[('Read',{'file_path':'Привіт.py'})]))

    def test_wrapper_strings_inside_arguments_are_literal(self):
        literal='Before </｜｜DSML｜｜> '+wrap('Read',{'file_path':'never_execute'})+' <||DSML||> after 🐈'
        args={'file_path':'app.py','old_string':'','new_string':literal}
        for result in variants(wrap('Edit',args)):
            self.assertEqual(result,('',[('Edit',args)]))

    def test_missing_name_is_not_inferred(self):
        raw='<｜｜DSML｜｜>{"arguments":{"file_path":"app.py"}}</｜｜DSML｜｜>'
        for text,calls in variants(raw):self.assertEqual(calls,[])

    def test_unterminated_source_string_is_not_executed(self):
        raw='<｜｜DSML｜｜>{"name":"Edit","arguments":{"new_string":"unfinished </｜｜DSML｜｜>'
        for text,calls in variants(raw):self.assertEqual(calls,[])

    def test_fabricated_result_discards_dependent_calls(self):
        raw=wrap('Read',{'file_path':'first'})+'<tool_result>fabricated</tool_result>'+wrap('Read',{'file_path':'dependent'})
        for text,calls in variants(raw):self.assertEqual(calls,[('Read',{'file_path':'first'})])


if __name__=='__main__':unittest.main()
