"""Recover surplus structural closers, never names, values or truncated calls."""
import json
import random
import unittest
from unittest.mock import patch
import emutools.engine as engine
from emutools import CanonRequest,CanonMessage,Config,ToolDef
from emutools.structured import StructuredToolParser,extract_structured_output


def broken(args=None,name='Read',peer=False):
    calls=[{'name':name,'arguments':args if args is not None else {'file_path':'app.py'},'id':'history'}]
    if peer:calls.append({'name':'Read','arguments':{'file_path':'peer.py'}})
    raw=json.dumps({'text':'','tool_calls':calls},ensure_ascii=False,separators=(',',':'))
    return raw.replace(',"id":"history"','],"id":"history"',1)


def chunks(raw):
    for pos in range(0,len(raw),3):yield {'text':raw[pos:pos+3]}
    yield {'usage':{'prompt_tokens':3,'completion_tokens':2}}
    yield {'finish':'stop'}


class SurplusTests(unittest.TestCase):
    def request(self):
        return CanonRequest(model='test',messages=[CanonMessage('user','Read the file')],tools=[ToolDef('Read',schema={'type':'object','properties':{'file_path':{'type':'string'}},'required':['file_path'],'additionalProperties':False})])

    def test_observed_extra_closer_is_opt_in(self):
        raw=broken()
        with self.assertRaises(ValueError):extract_structured_output(raw)
        text,calls=extract_structured_output(raw,salvage=True)
        self.assertEqual(text,'');self.assertEqual(len(calls),1)
        self.assertEqual(calls[0].name,'Read');self.assertEqual(calls[0].args,{'file_path':'app.py'})
        self.assertTrue(calls[0].repaired);self.assertNotEqual(calls[0].id,'history')

    def test_literal_values_are_unchanged(self):
        rng=random.Random(607)
        alphabet='[]{}"\\\n\r\t<>/ Привіт🐈'
        for _ in range(200):
            literal=''.join(rng.choice(alphabet) for _ in range(60))
            args={'source':literal,'nested':[True,None,42,{'text':literal}]}
            _,calls=extract_structured_output(broken(args),salvage=True)
            self.assertEqual(calls[0].args,args)

    def test_truncated_peer_releases_nothing(self):
        raw=broken(peer=True)[:-8]
        parser=StructuredToolParser(salvage=True)
        for char in raw:self.assertEqual(parser.feed(char),[])
        self.assertEqual(parser.finish(),([],[]));self.assertTrue(parser.error)

    def test_missing_fields_and_unsafe_numbers_are_not_invented(self):
        examples=[broken().replace('"name":"Read",',''),broken().replace('"arguments":','"wrong":'),
                  broken().replace('"name":"Read"','"name":"Read","name":"Bash"'),
                  broken({'n':float('nan')}),broken({'n':1.0}).replace('1.0','1e999'),
                  broken().replace('"app.py"','"unterminated'),
                  '{"text":"","tool_calls":[]} ]']
        for raw in examples:
            with self.subTest(raw=raw):
                with self.assertRaises(ValueError):extract_structured_output(raw,salvage=True)

    def test_more_than_two_surplus_closers_are_rejected(self):
        with self.assertRaises(ValueError):extract_structured_output(broken().replace('],"id"',']]],"id"'),salvage=True)

    def test_sync_honors_the_salvage_switch(self):
        response={'choices':[{'message':{'content':broken()},'finish_reason':'stop'}],'usage':{'prompt_tokens':3,'completion_tokens':2}}
        with patch.object(engine,'upstream_complete',return_value=response) as upstream:
            result=engine.run_turn(self.request(),Config(json_output=True))
            self.assertEqual(result.calls[0].args,{'file_path':'app.py'})
            self.assertTrue(result.calls[0].repaired);self.assertEqual(upstream.call_count,1)
        with patch.object(engine,'upstream_complete',return_value=response):
            with self.assertRaises(engine.UpstreamError):engine.run_turn(self.request(),Config(json_output=True,salvage_bare_json=False,loop_retry=False))

    def test_stream_keeps_salvage_switch_schema_and_call_limits(self):
        with patch.object(engine,'upstream_stream',side_effect=lambda *_:chunks(broken(peer=True))):
            events=list(engine.run_turn_stream(self.request(),Config(json_output=True,parallel=True,max_calls_per_turn=1)))
        self.assertEqual(sum(k=='call' for k,v in events),1)
        for raw,cfg in [(broken(),Config(json_output=True,salvage_bare_json=False,loop_retry=False)),
                        (broken({'file_path':17}),Config(json_output=True,loop_retry=False)),
                        (broken(name='Unknown'),Config(json_output=True,loop_retry=False))]:
            with patch.object(engine,'upstream_stream',side_effect=lambda *_:chunks(raw)):
                events=list(engine.run_turn_stream(self.request(),cfg))
            self.assertFalse(any(k=='call' for k,v in events))
            self.assertTrue(any(k=='text' and '[tool guard] Rejected tool call:' in v for k,v in events))


if __name__=='__main__':unittest.main()
