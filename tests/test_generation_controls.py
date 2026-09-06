"""Explicit provider generation controls remain per-server and opt-in."""
import os
import unittest
from unittest.mock import patch
from emutools.core import Config,CanonRequest,CanonMessage,ToolDef
from emutools import engine


class GenerationControlsTests(unittest.TestCase):
    def payload(self,cfg):
        request=CanonRequest(model='deepseek-v4-pro',messages=[CanonMessage('user','Work')],tools=[ToolDef('Read')])
        return engine._turn_payload(request,cfg,[],True)
    def test_default_preserves_provider_defaults(self):
        payload=self.payload(Config(thinking='',reasoning_effort=''))
        self.assertNotIn('thinking',payload);self.assertNotIn('reasoning_effort',payload)
    def test_explicitly_disable_thinking_without_native_tools(self):
        payload=self.payload(Config(thinking='disabled'))
        self.assertEqual(payload['thinking'],{'type':'disabled'})
        self.assertNotIn('tools',payload);self.assertNotIn('functions',payload)
    def test_explicit_thinking_effort(self):
        payload=self.payload(Config(thinking='enabled',reasoning_effort='low'))
        self.assertEqual(payload['thinking'],{'type':'enabled'});self.assertEqual(payload['reasoning_effort'],'low')
    def test_invalid_controls_are_rejected(self):
        for field in ('thinking','reasoning_effort'):
            with self.assertRaises(ValueError):Config(**{field:'typo'})
    def test_settings_are_captured_per_instance(self):
        with patch.dict(os.environ,{'EMU_THINKING':' disabled ','EMU_REASONING_EFFORT':'low'}):first=Config()
        with patch.dict(os.environ,{'EMU_THINKING':'enabled','EMU_REASONING_EFFORT':'max'}):second=Config()
        self.assertEqual(self.payload(first)['thinking'],{'type':'disabled'})
        self.assertEqual(self.payload(second)['thinking'],{'type':'enabled'})
        self.assertEqual(self.payload(first)['reasoning_effort'],'low')


if __name__=='__main__':unittest.main()
