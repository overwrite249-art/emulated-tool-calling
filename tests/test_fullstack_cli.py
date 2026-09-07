"""Benchmark configuration errors must fail before any paid work."""
import importlib.util
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

RUN=Path(__file__).resolve().parents[1]/'benchmarks/fullstack/run.py'

class FullstackCliTests(unittest.TestCase):
    def test_invalid_output_allowance_creates_no_workspace(self):
        with tempfile.TemporaryDirectory() as directory:
            out=Path(directory)/'never-created'
            for value in ('0','-1','6001','not-an-integer'):
                with self.subTest(value=value):
                    result=subprocess.run([sys.executable,str(RUN),'--cli','not-invoked','--out-dir',str(out),'--max-output-tokens',value],env=dict(os.environ,EMU_UPSTREAM_API_KEY='test-only-never-sent'),capture_output=True,text=True,timeout=10)
                    self.assertEqual(result.returncode,2)
                    self.assertIn('--max-output-tokens',result.stderr)
                    self.assertFalse(out.exists())
    def test_invalid_reasoning_effort_creates_no_workspace(self):
        with tempfile.TemporaryDirectory() as directory:
            out=Path(directory)/'never-created'
            result=subprocess.run([sys.executable,str(RUN),'--cli','not-invoked','--out-dir',str(out),'--reasoning-effort','unlimited'],env=dict(os.environ,EMU_UPSTREAM_API_KEY='test-only-never-sent'),capture_output=True,text=True,timeout=10)
            self.assertEqual(result.returncode,2)
            self.assertFalse(out.exists())
    def test_invalid_result_allowance_creates_no_workspace(self):
        with tempfile.TemporaryDirectory() as directory:
            out=Path(directory)/'never-created'
            for value in ('0','-1','24001','not-an-integer'):
                with self.subTest(value=value):
                    result=subprocess.run([sys.executable,str(RUN),'--cli','not-invoked','--out-dir',str(out),'--max-result-chars',value],env=dict(os.environ,EMU_UPSTREAM_API_KEY='test-only-never-sent'),capture_output=True,text=True,timeout=10)
                    self.assertEqual(result.returncode,2)
                    self.assertIn('--max-result-chars',result.stderr)
                    self.assertFalse(out.exists())
    def test_result_limit_reaches_the_actual_proxy_configuration(self):
        from emutools import Config
        with patch.object(sys,'path',[str(RUN.parent)]+sys.path):
            spec=importlib.util.spec_from_file_location('fullstack_settings_test',RUN)
            module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        for limit in (1,4096,24000):
            args=SimpleNamespace(max_result_chars=limit,json_output=False,thinking='disabled',reasoning_effort=None)
            env=module.proxy_settings(args)
            self.assertNotIn('EMU_UPSTREAM_API_KEY',env)
            with patch.dict(os.environ,env,clear=True):cfg=Config()
            self.assertEqual(cfg.max_result_chars,limit)
            self.assertFalse(cfg.json_output)
            self.assertEqual(cfg.thinking,'disabled')
    def test_help_documents_focused_continuations(self):
        result=subprocess.run([sys.executable,str(RUN),'--help'],capture_output=True,text=True,timeout=10)
        self.assertEqual(result.returncode,0)
        for flag in ('--focus','--resume-app','--thinking','--max-output-tokens','--max-result-chars','--json-output','--reasoning-effort'):
            self.assertIn(flag,result.stdout)

if __name__=='__main__':unittest.main()
