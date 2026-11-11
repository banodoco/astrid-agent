import json, pathlib, tempfile, unittest
from sanitize import sanitized_result, write_receipt
A='tests/test_safe.py::test_a'; B='tests/test_safe.py::test_b'; SECRET='synthetic_token_8e64714e_NOT_FOR_ARTIFACT'
def make(raw, expected=(A,B), exit_code=1):
    return sanitized_result(raw.encode(),list(expected),[],{'side':'base'},exit_code,True)
class Sanitization(unittest.TestCase):
    def test_safe_statuses_and_counts_only(self):
        raw=f'{A} PASSED [ 50%]\n{B} FAILED [100%]\nFAILED {B} - {SECRET}\ntraceback: {SECRET}\n=== 1 failed, 1 passed in 1.20s ===\n'
        result=make(raw)
        self.assertEqual(result['classification'],'COMPLETE'); self.assertEqual(result['outcome_counts'],{'FAILED':1,'PASSED':1}); self.assertEqual(result['summary_counts'],{'failed':1,'passed':1}); self.assertNotIn(SECRET,json.dumps(result))
    def test_injected_newlines_and_unknown_ids_never_copied(self):
        raw=f'{A} PASSED\n{B} FAILED\n{SECRET}\ntests/test_safe.py::test_{SECRET} FAILED\n=== 1 failed, 1 passed in 1.0s ===\n'
        result=make(raw); self.assertEqual(result['classification'],'INCOMPLETE'); self.assertEqual(result['unexpected_result_count'],1); self.assertNotIn(SECRET,json.dumps(result))
    def test_missing_duplicate_and_skip_are_accurate(self):
        result=make(f'{A} SKIPPED\n{A} SKIPPED\n=== 2 skipped in 1.0s ===\n',exit_code=0)
        self.assertEqual(result['classification'],'INCOMPLETE'); self.assertEqual(result['missing_ids'],[B]); self.assertEqual(result['duplicate_ids'],[A]); self.assertEqual(result['outcomes'],[{'id':A,'status':'SKIPPED'},{'id':A,'status':'SKIPPED'}])
        result=make(f'{A} SKIPPED\n{B} PASSED\n=== 1 skipped, 1 passed in 1.0s ===\n',exit_code=0)
        self.assertEqual(result['classification'],'COMPLETE'); self.assertIn({'id':A,'status':'SKIPPED'},result['outcomes'])
    def test_newline_injection_cannot_be_a_summary(self):
        raw=f'{A} PASSED\n{B} FAILED\n=== 1 failed, 1 passed {SECRET} in 1.0s ===\n'
        result=make(raw); self.assertEqual(result['classification'],'INCOMPLETE'); self.assertIsNone(result['summary_counts']); self.assertNotIn(SECRET,json.dumps(result))
    def test_only_structured_file_is_uploaded(self):
        root=pathlib.Path(__file__).resolve().parents[2]; workflow=(root/'.github/workflows/r12-a2-once.yml').read_text()
        self.assertIn('path: ${{ runner.temp }}/r12-a2-receipt/receipt.json',workflow)
        with tempfile.TemporaryDirectory() as tmp:
            root=pathlib.Path(tmp); public=root/'public'; private=root/'private'; public.mkdir(); private.mkdir()
            (private/'pytest.log').write_text(SECRET); (private/'setup.log').write_text(SECRET)
            write_receipt(public,make(f'{A} PASSED\n{B} FAILED\n=== 1 failed, 1 passed in 1.0s ===\n'))
            self.assertEqual([p.name for p in public.iterdir()],['receipt.json']); self.assertNotIn(SECRET,(public/'receipt.json').read_text())
    def test_missing_or_inconsistent_summary_and_exit_fail_closed(self):
        raw=f'{A} PASSED\n{B} FAILED\n=== 1 failed, 1 passed in 1.0s ===\n'
        self.assertEqual(make(raw,exit_code=0)['classification'],'INCOMPLETE')
        self.assertEqual(make(raw.replace('1 failed','2 failed'))['classification'],'INCOMPLETE')
        self.assertEqual(make(raw.replace('=== 1 failed, 1 passed in 1.0s ===',''))['classification'],'INCOMPLETE')
if __name__=='__main__': unittest.main()
