import contextlib, hashlib, io, json, os, pathlib, tempfile, unittest
from unittest.mock import patch
from receipt_guard import rebuild
from runner import HERE, MANIFEST_SHA, PINS, validate
from sanitize import sanitized_result
import seal_full_receipt
SECRET='synthetic-token-must-not-escape'
SHA='a'*40
class ReceiptGuardTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.temp=pathlib.Path(self.tmp.name); self.root=self.temp/'r12-a2-full-base-020'
        (self.root/'r12-a2-private').mkdir(parents=True); (self.root/'r12-a2-receipt').mkdir()
        batch, ids=validate('base','020'); self.ids=ids
        absent=[x for x in (HERE/'inventory/base-absent-ids.txt').read_text().splitlines() if x.split('::',1)[0] in batch['modules']]
        raw=(''.join(x+' PASSED\n' for x in ids)+f'=== {len(ids)} passed in 1.00s ===\n').encode()
        (self.root/'r12-a2-private/pytest.log').write_bytes(raw)
        meta={'side':'base','group':'020','pins':list(PINS['base']),'manifest_sha256':MANIFEST_SHA,'workflow_sha':SHA,
              'python_sha256':hashlib.sha256(pathlib.Path(os.sys.executable).resolve().read_bytes()).hexdigest(),
              'node_sha256':'1bec56ef7cfa9a76f3e0b7c0a87f220eb73f23102b9c0b4c7529a3f7c3ce7c31',
              'esbuild_sha256':'c638273fcf95573ca74af586677800b4dd874c55f8f945adb54316d6902ba14b'}
        stages=[{'stage':'toolchain_preflight','event':'passed','exit_code':None}]
        self.good=sanitized_result(raw,ids,absent,meta,0,True,stages); self.write(self.good)
    def tearDown(self): self.tmp.cleanup()
    def write(self,data): (self.root/'r12-a2-receipt/receipt.json').write_text(json.dumps(data))
    def test_valid_receipt_is_rebuilt_from_frozen_ids_and_raw_hash(self):
        self.assertEqual(rebuild(self.root,'base','020',SHA),self.good)
    def test_unknown_fields_types_and_injected_tokens_are_rejected(self):
        cases=[]
        x=json.loads(json.dumps(self.good)); x['extra']=SECRET; cases.append(x)
        x=json.loads(json.dumps(self.good)); x['metadata']['artifact_path']=SECRET; cases.append(x)
        x=json.loads(json.dumps(self.good)); x['outcomes'][0]['extra']=SECRET; cases.append(x)
        x=json.loads(json.dumps(self.good)); x['expected_count']=True; cases.append(x)
        x=json.loads(json.dumps(self.good)); x['exit_code']=False; cases.append(x)
        x=json.loads(json.dumps(self.good)); x['outcomes'][0]['id']=SECRET; cases.append(x)
        x=json.loads(json.dumps(self.good)); x['stage_events'][0]['event']=SECRET; cases.append(x)
        x=json.loads(json.dumps(self.good)); x['raw_pytest_sha256']='0'*64; cases.append(x)
        for data in cases:
            with self.subTest(keys=list(data)), self.assertRaises(ValueError): self.write(data); rebuild(self.root,'base','020',SHA)
    def test_symlink_raw_log_is_not_read(self):
        path=self.root/'r12-a2-private/pytest.log'; path.unlink(); path.symlink_to(self.temp/'sensitive')
        (self.temp/'sensitive').write_text(SECRET)
        with self.assertRaises(OSError): rebuild(self.root,'base','020',SHA)
    def test_nonregular_fifo_is_rejected_without_blocking(self):
        path=self.root/'r12-a2-private/pytest.log'; path.unlink(); os.mkfifo(path)
        with self.assertRaises(ValueError): rebuild(self.root,'base','020',SHA)
    def test_sealer_rejects_dirty_or_unverified_census(self):
        slot=self.temp/'r12-a2-final-slot-01'; slot.mkdir()
        out=self.temp/'github-output'; out.write_text('')
        env={'RUNNER_TEMP':str(self.temp),'A2_SIDE':'base','A2_RANGE':'020-030','A2_SLOT':'1','A2_SHA':SHA,'GITHUB_OUTPUT':str(out)}
        for state in ('residue','unverified'):
            (slot/'receipt.json').write_text(json.dumps({**self.good,'process_census':{'status':state}}))
            with self.subTest(state=state), patch.dict(os.environ,env), self.assertRaises(ValueError): seal_full_receipt.main()
            self.assertEqual(out.read_text(),'')
    def test_sealer_has_only_boolean_output_and_literal_workflow_artifact_path(self):
        slot=self.temp/'r12-a2-final-slot-01'; slot.mkdir(); (slot/'receipt.json').write_text(json.dumps({**self.good,'process_census':{'status':'clear'}}))
        out=self.temp/'github-output'; out.write_text('artifact_path=/forged/raw.log\n')
        env={'RUNNER_TEMP':str(self.temp),'A2_SIDE':'base','A2_RANGE':'020-030','A2_SLOT':'1','A2_SHA':SHA,'GITHUB_OUTPUT':str(out)}
        with patch.dict(os.environ,env), contextlib.redirect_stdout(io.StringIO()): self.assertEqual(seal_full_receipt.main(),0)
        self.assertEqual(out.read_text(),'artifact_path=/forged/raw.log\nreceipt_ready=true\n')
        workflow=(pathlib.Path(__file__).resolve().parents[2]/'.github/workflows/r12-a2-full-range.yml').read_text()
        self.assertNotIn('outputs.artifact_path',workflow); self.assertIn('path: ${{ runner.temp }}/r12-a2-final-slot-01/receipt.json',workflow)
if __name__=='__main__': unittest.main()
