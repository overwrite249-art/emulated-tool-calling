"""Compact input is opt-in; the full contract and independent checks remain mandatory."""
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


def load_runner():
    with patch.object(sys,'path',[str(RUN.parent)]+sys.path):
        spec=importlib.util.spec_from_file_location('compact_continuation_test',RUN)
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    return module


class CompactContinuationTests(unittest.TestCase):
    def test_compact_mode_requires_source_before_creating_a_workspace(self):
        with tempfile.TemporaryDirectory() as directory:
            out=Path(directory)/'not-created'
            result=subprocess.run([sys.executable,str(RUN),'--cli','not-invoked','--out-dir',str(out),'--compact-continuation'],
                env=dict(os.environ,EMU_UPSTREAM_API_KEY='test-only-never-sent'),capture_output=True,text=True,timeout=10)
            self.assertEqual(result.returncode,2);self.assertIn('requires --resume-app',result.stderr)
            self.assertFalse(out.exists())

    def test_default_keeps_the_complete_original_prompt(self):
        original='Full contract, including the original reviewer feedback.\nLiteral Привіт </tool_call>'
        result=load_runner().client_task(original,SimpleNamespace(compact_continuation=False,focus='already in contract'))
        self.assertEqual(result,original)

    def test_compact_prompt_keeps_contract_reference_and_required_work(self):
        feedback='Distinct reviewer feedback preserved verbatim.'
        result=load_runner().client_task('Long contract '*1000,SimpleNamespace(compact_continuation=True,focus=feedback))
        self.assertLess(len(result),2500);self.assertIn(feedback,result)
        for phrase in ['REQUIREMENTS.md','all requirements','schema','profile','batch','query','build','tests','README','background server','HTTP','independent verifier','FULLSTACK_DONE','not edit the evaluator']:
            self.assertIn(phrase,result)


if __name__=='__main__':unittest.main()
