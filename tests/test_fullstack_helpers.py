"""Offline tests for the opt-in paid benchmark's accounting and continuation."""
import hashlib
from pathlib import Path
import tempfile
import unittest
from benchmarks.fullstack.budget import usage_upper_bound
from benchmarks.fullstack.source import copy_source


class BudgetTests(unittest.TestCase):
    def cost(self,**kw):return usage_upper_bound(dict(prompt_tokens=100,completion_tokens=50,**kw),.25)
    def test_cache_miss_default(self):self.assertAlmostEqual(self.cost(),.00033)
    def test_verified_cache_hits(self):self.assertAlmostEqual(self.cost(prompt_cache_hit_tokens=80,prompt_cache_miss_tokens=20),.00022792)
    def test_invalid_cache_hit_metrics_get_no_discount(self):
        for hit in (-1,101,True,'80'):self.assertAlmostEqual(self.cost(prompt_cache_hit_tokens=hit),.00033)
    def test_inconsistent_cache_metrics_get_no_discount(self):self.assertAlmostEqual(self.cost(prompt_cache_hit_tokens=80,prompt_cache_miss_tokens=40),.00033)
    def test_unknown_usage_uses_full_reservation(self):
        for usage in (None,{},dict(prompt_tokens=-1,completion_tokens=10),dict(prompt_tokens=100,completion_tokens=True)):
            self.assertEqual(usage_upper_bound(usage,.25),.25)


class SourceTests(unittest.TestCase):
    def test_source_and_hashes_preserved_not_database_or_build(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);source=root/'source';dest=root/'dest';source.mkdir();dest.mkdir()
            for name in ('app.py','web/app.ts','data/inventory.sqlite','dist/app.js','REQUIREMENTS.md','acceptance-1/result.json'):
                p=source/name;p.parent.mkdir(parents=True,exist_ok=True);p.write_text('model-generated example')
            copied=copy_source(source,dest)
            self.assertEqual([r['path'] for r in copied],['app.py','web/app.ts'])
            self.assertEqual(copied[0]['sha256'],hashlib.sha256((source/'app.py').read_bytes()).hexdigest())
            self.assertFalse((dest/'data').exists());self.assertFalse((dest/'dist').exists())
    def test_symlinks_are_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);source=root/'source';dest=root/'dest';source.mkdir();dest.mkdir();(root/'outside').write_text('not source')
            (source/'app.py').symlink_to(root/'outside')
            with self.assertRaises(ValueError):copy_source(source,dest)
    def test_overlapping_directories_are_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d)
            for dest in (root,root/'child'):
                with self.assertRaises(ValueError):copy_source(root,dest)


if __name__=='__main__':unittest.main()
