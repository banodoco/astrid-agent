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
    def test_extra_malformed_outcome_lines_fail_closed(self):
        good=f'{A} PASSED\n{B} FAILED\n=== 1 failed, 1 passed in 1.0s ===\n'
        for injected in (f'{A} FAILED {SECRET}',f'{B} PASSED [100%] {SECRET}',f'tests/other.py::test_new SKIPPED ({SECRET})',f'{A} passed',f'  {A} FAILED'):
            with self.subTest(injected=injected):
                result=make(good+injected+'\n'); self.assertEqual(result['classification'],'INCOMPLETE'); self.assertEqual(result['malformed_outcome_count'],1); self.assertNotIn(SECRET,json.dumps(result))
    def test_extra_malformed_summaries_fail_closed(self):
        good=f'{A} PASSED\n{B} FAILED\n=== 1 failed, 1 passed in 1.0s ===\n'
        for injected in (f'=== 1 failed {SECRET} in 1.0s ===', '=== 2 passed in nope ===', '=== 1 passed, 1 passed in 1.0s ===', '=== 1 failure', '== 1 passed in nope =='):
            with self.subTest(injected=injected):
                result=make(good+injected+'\n'); self.assertEqual(result['classification'],'INCOMPLETE'); self.assertEqual(result['malformed_summary_count'],1); self.assertNotIn(SECRET,json.dumps(result))
    def test_ordinary_chatter_is_ignored(self):
        raw=f'=== test session starts ===\nplatform linux -- Python 3.11\n{A} PASSED\n{B} FAILED\n=== FAILURES ===\ntraceback {SECRET}\n=== short test summary info ===\nFAILED {B} - {SECRET}\n=== 1 failed, 1 passed in 1.0s ===\n'
        result=make(raw); self.assertEqual(result['classification'],'COMPLETE'); self.assertNotIn(SECRET,json.dumps(result))
    def test_short_real_group_022_ids_accept_pytest_ascii_padding(self):
        tag='tests/core/pack/test_install.py::test_reachable_revisions[tag]'
        head='tests/core/pack/test_install.py::test_reachable_revisions[head]'
        raw=f'{tag} PASSED    [ 12%]\n{head} PASSED   [ 13%]\n=== 2 passed in 1.0s ===\n'
        result=make(raw,expected=(tag,head),exit_code=0)
        self.assertEqual(result['classification'],'COMPLETE'); self.assertEqual(result['missing_ids'],[])
        self.assertEqual(result['malformed_outcome_count'],0); self.assertEqual(result['incomplete_reasons'],[])
    def test_progress_padding_rejects_tabs_suffixes_and_injected_lines(self):
        tag='tests/core/pack/test_install.py::test_reachable_revisions[tag]'
        head='tests/core/pack/test_install.py::test_reachable_revisions[head]'
        for forged in (f'{tag} PASSED  \t[ 12%]',f'{tag} PASSED    [ 12%] {SECRET}',f'{tag} PASSED    [ 12%]\n{tag} FAILED {SECRET}'):
            raw=f'{forged}\n{head} PASSED   [ 13%]\n=== 2 passed in 1.0s ===\n'
            with self.subTest(forged=forged):
                result=make(raw,expected=(tag,head),exit_code=0)
                self.assertEqual(result['classification'],'INCOMPLETE')
                self.assertIn('MALFORMED_OUTCOME',result['incomplete_reasons'])
                self.assertNotIn(SECRET,json.dumps(result))
    def test_incomplete_reason_codes_are_fixed(self):
        result=make(f'{A} PASSED\n=== 1 passed in 1.0s ===\n',exit_code=0)
        self.assertEqual(result['classification'],'INCOMPLETE'); self.assertIn('MISSING_IDS',result['incomplete_reasons'])
        self.assertNotIn(B, result['incomplete_reasons'])
    def test_outcome_then_teardown_error_is_accounted_once_with_both_statuses(self):
        raw=f'{A} PASSED [ 50%]\n{A} ERROR [ 50%]\n{B} PASSED [100%]\n=== 2 passed, 1 error in 1.0s ===\n'
        result=make(raw,exit_code=1)
        self.assertEqual(result['classification'],'COMPLETE'); self.assertEqual(result['duplicate_ids'],[])
        self.assertEqual(result['additional_error_after_outcome_ids'],[A])
        self.assertEqual([x['status'] for x in result['outcomes'] if x['id']==A],['PASSED','ERROR'])
        self.assertEqual(result['outcome_counts'],{'ERROR':1,'PASSED':2})
    def test_only_outcome_then_single_error_is_valid(self):
        for statuses in (('PASSED','PASSED'),('ERROR','ERROR'),('ERROR','PASSED'),('PASSED','ERROR','ERROR'),('FAILED','FAILED')):
            raw=''.join(f'{A} {status}\n' for status in statuses)+f'{B} PASSED\n=== 1 failed, 1 passed, 1 error in 1.0s ===\n'
            with self.subTest(statuses=statuses):
                result=make(raw,exit_code=1); self.assertEqual(result['classification'],'INCOMPLETE'); self.assertIn(A,result['duplicate_ids'])
        raw=f'{A} FAILED\n{A} ERROR\n{B} PASSED\n=== 1 failed, 1 passed, 1 error in 1.0s ===\n'
        self.assertEqual(make(raw,exit_code=1)['classification'],'COMPLETE')
        self.assertEqual(make(raw.replace('1 error','2 errors'),exit_code=1)['classification'],'INCOMPLETE')
        self.assertEqual(make(raw,exit_code=0)['classification'],'INCOMPLETE')
    def test_known_local_evidence_skip_reason_only(self):
        known='tests/test_installer_r0_entry.py::test_captures_match_real_r0_evidence'
        raw=f'{known} SKIPPED (local R0 evidence is not present) [ 50%]\n{B} PASSED [100%]\n=== 1 passed, 1 skipped in 1.0s ===\n'
        result=make(raw,expected=(known,B),exit_code=0)
        self.assertEqual(result['classification'],'COMPLETE'); self.assertEqual(result['outcomes'][0],{'id':known,'status':'SKIPPED'})
        self.assertNotIn('local R0 evidence is not present',json.dumps(result))
        for forged in (raw.replace('local R0 evidence is not present',SECRET), raw.replace(known,A),raw.replace(') [ 50%]',f') {SECRET} [ 50%]')):
            r=make(forged,expected=(known,B),exit_code=0); self.assertEqual(r['classification'],'INCOMPLETE'); self.assertNotIn(SECRET,json.dumps(r))
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
