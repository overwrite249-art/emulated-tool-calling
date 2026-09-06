"""Benchmark configuration errors must fail before any paid work."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

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
    def test_help_documents_focused_continuations(self):
        result=subprocess.run([sys.executable,str(RUN),'--help'],capture_output=True,text=True,timeout=10)
        self.assertEqual(result.returncode,0)
        for flag in ('--focus','--resume-app','--thinking','--max-output-tokens','--json-output','--reasoning-effort'):
            self.assertIn(flag,result.stdout)

if __name__=='__main__':unittest.main()
