"""The benchmark must pass the requested older-result limit to the real Config."""
import importlib.util
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from emutools import Config

RUN=Path(__file__).resolve().parents[1]/'benchmarks/fullstack/run.py'


def runner():
    with patch.object(sys,'path',[str(RUN.parent)]+sys.path):
        spec=importlib.util.spec_from_file_location('history_limit_cli_test',RUN)
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    return module


class HistoryLimitCliTests(unittest.TestCase):
    def test_invalid_history_limit_creates_no_workspace(self):
        for value in ['-1','24001']:
            with tempfile.TemporaryDirectory() as directory:
                out=Path(directory)/'not-created'
                result=subprocess.run([sys.executable,str(RUN),'--cli','never-invoked','--out-dir',str(out),'--history-result-chars',value],
                    env=dict(os.environ,EMU_UPSTREAM_API_KEY='test-only-never-sent'),capture_output=True,text=True,timeout=10)
                self.assertEqual(result.returncode,2)
                self.assertIn('--history-result-chars must be between 0 and 24000',result.stderr)
                self.assertFalse(out.exists())

    def test_requested_limit_reaches_actual_proxy_configuration(self):
        module=runner()
        for value in [0,1,512,24000]:
            args=SimpleNamespace(json_output=False,max_result_chars=8192,history_result_chars=value,thinking=None,reasoning_effort=None)
            settings=module.proxy_settings(args)
            with patch.dict(os.environ,settings,clear=True):self.assertEqual(Config().history_result_chars,value)
            self.assertNotIn('EMU_UPSTREAM_API_KEY',settings)


if __name__=='__main__':unittest.main()
