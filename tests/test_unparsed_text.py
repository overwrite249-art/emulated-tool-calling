"""Real run-12 text fixtures: reject malformed envelopes, never invent calls."""
import unittest
from unittest.mock import patch
import emutools.engine as engine
from emutools import Config,CanonMessage,CanonRequest,ToolDef,ToolCall,render_tool_call_text

A='mcp__warehouse__db_schema'
B='mcp__warehouse__db_profile'
OBSERVED_ONE=('I\'ll start by inspecting the database schema and profile in parallel, then review the existing app files.\n\n'
 '<tool_call>\n<tool_call name="name">mcp__warehouse__db_schema</tool_call>\n'
 '<parameter name="arguments">{}</parameter>\n</tool_call>\n</tool_calls>')
OBSERVED_TWO=('<tool_calls>\n<tool_calls>\n<mcp__warehouse__db_schema>\n</mcp__warehouse__db_schema>\n'
 '<mcp__warehouse__db_profile>\n</mcp__warehouse__db_profile>')
GOOD='\n'.join(render_tool_call_text(ToolCall(name,{})) for name in [A,B])
USAGE={'prompt_tokens':11,'completion_tokens':7}


def request(**kwargs):
    tools=[ToolDef(name,schema={'type':'object','properties':{},'additionalProperties':False}) for name in [A,B]]
    return CanonRequest(model='test',messages=[CanonMessage('user','Inspect the database')],tools=tools,**kwargs)


def settings(**kwargs):
    return Config(parallel=True,max_calls_per_turn=4,image_inputs=False,**kwargs)


def streamed(text,width=7):
    for index in range(0,len(text),width):yield {'text':text[index:index+width]}
    yield {'usage':dict(USAGE)};yield {'finish':'stop'}


def completed(text):
    return {'choices':[{'message':{'content':text},'finish_reason':'stop'}],'usage':dict(USAGE)}


class UnparsedTextTests(unittest.TestCase):
    def test_observed_sequence_reaches_third_stream_attempt(self):
        for width in [1,2,7,64,100000]:
            with self.subTest(width=width):
                responses=iter([OBSERVED_ONE,OBSERVED_TWO,GOOD])
                with patch.object(engine,'upstream_stream',side_effect=lambda *_:streamed(next(responses),width)) as upstream:
                    events=list(engine.run_turn_stream(request(stream=True),settings()))
                self.assertEqual(upstream.call_count,3)
                self.assertEqual([(v.name,v.args) for k,v in events if k=='call'],[(A,{}),(B,{})])
                self.assertEqual([v for k,v in events if k=='usage'],[{'prompt_tokens':33,'completion_tokens':21}])
                self.assertEqual([v for k,v in events if k=='finish'],['tool_calls'])

    def test_observed_sequence_reaches_third_sync_attempt(self):
        responses=iter([OBSERVED_ONE,OBSERVED_TWO,GOOD])
        with patch.object(engine,'upstream_complete',side_effect=lambda *_:completed(next(responses))) as upstream:
            result=engine.run_turn(request(),settings())
        self.assertEqual(upstream.call_count,3);self.assertEqual(result.attempts,3)
        self.assertEqual([(c.name,c.args) for c in result.calls],[(A,{}),(B,{})])
        self.assertEqual(result.usage,{'prompt_tokens':33,'completion_tokens':21})

    def test_unknown_wrapper_never_becomes_an_invented_call(self):
        for mode in ['sync','stream']:
            with self.subTest(mode=mode):
                name='upstream_complete' if mode=='sync' else 'upstream_stream'
                value=lambda *_:completed(OBSERVED_TWO) if mode=='sync' else streamed(OBSERVED_TWO)
                with patch.object(engine,name,side_effect=value) as upstream:
                    with self.assertRaisesRegex(engine.UpstreamError,'no usable response'):
                        if mode=='sync':engine.run_turn(request(),settings())
                        else:list(engine.run_turn_stream(request(stream=True),settings()))
                self.assertEqual(upstream.call_count,3)

    def test_retry_disabled_is_an_explicit_rejection(self):
        with patch.object(engine,'upstream_complete',return_value=completed(OBSERVED_TWO)) as upstream:
            result=engine.run_turn(request(),settings(loop_retry=False))
        self.assertEqual(upstream.call_count,1);self.assertFalse(result.calls);self.assertTrue(result.notes)
        self.assertIn('without executing',result.text)
        with patch.object(engine,'upstream_stream',side_effect=lambda *_:streamed(OBSERVED_TWO)) as upstream:
            events=list(engine.run_turn_stream(request(stream=True),settings(loop_retry=False)))
        self.assertEqual(upstream.call_count,1);self.assertFalse([v for k,v in events if k=='call'])
        self.assertIn('[tool guard]',''.join(v for k,v in events if k=='text'))

    def test_prose_before_a_wrapper_does_not_hide_failure(self):
        raw='I will inspect the database.\n'+OBSERVED_TWO
        for mode in ['sync','stream']:
            responses=iter([raw,GOOD]);name='upstream_complete' if mode=='sync' else 'upstream_stream'
            with patch.object(engine,name,side_effect=lambda *_:completed(next(responses)) if mode=='sync' else streamed(next(responses),1)) as upstream:
                result=engine.run_turn(request(),settings()) if mode=='sync' else list(engine.run_turn_stream(request(stream=True),settings()))
            self.assertEqual(upstream.call_count,2)

    def test_long_leading_whitespace_and_crlf_are_bounded(self):
        responses=iter([' '*12000+OBSERVED_TWO.replace('\n','\r\n'),GOOD])
        with patch.object(engine,'upstream_stream',side_effect=lambda *_:streamed(next(responses),7)) as upstream:
            events=list(engine.run_turn_stream(request(stream=True),settings()))
        self.assertEqual(upstream.call_count,2);self.assertEqual(len([v for k,v in events if k=='call']),2)

    def test_no_repairs_when_tools_are_disabled(self):
        for req in [request(tool_choice='none'),CanonRequest(model='test',messages=[CanonMessage('user','Explain the syntax')])]:
            with patch.object(engine,'upstream_stream',side_effect=lambda *_:streamed(OBSERVED_TWO)) as upstream:
                events=list(engine.run_turn_stream(req,settings()))
            self.assertEqual(upstream.call_count,1);self.assertFalse([v for k,v in events if k=='call'])
            with patch.object(engine,'upstream_complete',return_value=completed(OBSERVED_TWO)) as upstream:
                result=engine.run_turn(req,settings())
            self.assertEqual(upstream.call_count,1);self.assertFalse(result.calls)

    def test_fenced_examples_and_prose_mentions_are_not_retried(self):
        for raw in ['```xml\n'+OBSERVED_TWO+'\n```','~~~~xml\n'+OBSERVED_TWO+'\n~~~~',
                    'The <tool_calls> container is not itself a call.',
                    '<tool_calls> is the literal name of the container.',
                    '{"note":"The <tool_calls> marker is data here."}']:
            with self.subTest(raw=raw):
                with patch.object(engine,'upstream_stream',side_effect=lambda *_:streamed(raw,1)) as upstream:
                    events=list(engine.run_turn_stream(request(stream=True),settings()))
                self.assertEqual(upstream.call_count,1);self.assertFalse([v for k,v in events if k=='call'])
                with patch.object(engine,'upstream_complete',return_value=completed(raw)) as upstream:
                    result=engine.run_turn(request(),settings())
                self.assertEqual(upstream.call_count,1);self.assertFalse(result.calls)

    def test_real_call_is_not_replayed_to_repair_a_peer(self):
        raw='<tool_calls>\n'+render_tool_call_text(ToolCall(A,{}))+'\n<mystery/>\n</tool_calls>'
        with patch.object(engine,'upstream_stream',side_effect=lambda *_:streamed(raw,1)) as upstream:
            events=list(engine.run_turn_stream(request(stream=True),settings()))
        self.assertEqual(upstream.call_count,1)
        self.assertEqual([(v.name,v.args) for k,v in events if k=='call'],[(A,{})])

    def test_valid_wrapped_batch_still_obeys_client_call_limit(self):
        raw='<tool_calls>\n'+GOOD+'\n</tool_calls>'
        with patch.object(engine,'upstream_stream',side_effect=lambda *_:streamed(raw,1)) as upstream:
            events=list(engine.run_turn_stream(request(stream=True,parallel_tool_calls=False),settings()))
        self.assertEqual(upstream.call_count,1);self.assertEqual(len([v for k,v in events if k=='call']),1)
        with patch.object(engine,'upstream_complete',return_value=completed(raw)) as upstream:
            result=engine.run_turn(request(parallel_tool_calls=False),settings())
        self.assertEqual(upstream.call_count,1);self.assertEqual(len(result.calls),1)

    def test_wrapped_calls_do_not_bypass_schema_validation(self):
        raw='<tool_calls>\n'+render_tool_call_text(ToolCall(A,{'not_allowed':True}))+'\n</tool_calls>'
        with patch.object(engine,'upstream_stream',side_effect=lambda *_:streamed(raw)):
            events=list(engine.run_turn_stream(request(stream=True),settings(loop_retry=False)))
        self.assertFalse([v for k,v in events if k=='call']);self.assertIn('[tool guard]',''.join(v for k,v in events if k=='text'))
        with patch.object(engine,'upstream_complete',return_value=completed(raw)):
            result=engine.run_turn(request(),settings(loop_retry=False))
        self.assertFalse(result.calls);self.assertTrue(result.notes)

    def test_literal_wrappers_in_arguments_remain_unchanged(self):
        value='<tool_calls>\n<not_a_call>Привіт</not_a_call>\n</tool_calls>'
        req=CanonRequest(model='test',messages=[CanonMessage('user','Echo data')],tools=[ToolDef('Echo',schema={'type':'object','properties':{'value':{'type':'string'}},'required':['value']})])
        raw=render_tool_call_text(ToolCall('Echo',{'value':value}))
        with patch.object(engine,'upstream_stream',side_effect=lambda *_:streamed(raw,1)) as upstream:
            events=list(engine.run_turn_stream(req,settings()))
        self.assertEqual(upstream.call_count,1);self.assertEqual([v.args for k,v in events if k=='call'],[{'value':value}])
        with patch.object(engine,'upstream_complete',return_value=completed(raw)) as upstream:
            result=engine.run_turn(req,settings())
        self.assertEqual(upstream.call_count,1);self.assertEqual([c.args for c in result.calls],[{'value':value}])


class PromptContractTests(unittest.TestCase):
    def test_default_prompt_does_not_advertise_a_conflicting_raw_format(self):
        from emutools.protocol import build_tool_prompt
        for parallel in [False,True]:
            prompt=build_tool_prompt([ToolDef('Read')],parallel)
            self.assertNotIn('## Raw form',prompt)
            self.assertNotIn('<arg name=',prompt)
            self.assertIn('"name"',prompt)
            self.assertIn('"arguments"',prompt)

    def test_parallel_prompt_does_not_order_a_stop_after_the_first_call(self):
        from emutools.protocol import build_tool_prompt
        prompt=build_tool_prompt([ToolDef('Read')],True)
        self.assertNotIn('STOP generating immediately after `</tool_call>`',prompt)
        self.assertIn('last tool-call block',prompt)
        self.assertIn('independent',prompt)
        self.assertIn('at most ONE',build_tool_prompt([ToolDef('Read')],False))


if __name__=='__main__':unittest.main()
