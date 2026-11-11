import hashlib, json, pathlib, shutil, tempfile, unittest
from unittest.mock import patch
import verify_prior

class PriorReceiptTests(unittest.TestCase):
    def test_all_56_original_bytes_and_schema_generations(self):
        self.assertTrue(verify_prior.verify_prior_receipts())
        index=json.loads((verify_prior.HERE/'DESIGN-080-hashes.json').read_text())
        self.assertEqual(len(index['prior_receipts']),56);self.assertEqual(len(index['remaining_pairs']),80)
        self.assertEqual(sum(r['has_incomplete_reasons'] for r in index['prior_receipts']),38)
        self.assertTrue(all(not r['has_additional_error_ids'] for r in index['prior_receipts']))
    def temp_copy(self,root):
        shutil.copytree(verify_prior.HERE/'prior-receipts',root/'prior-receipts')
        shutil.copytree(verify_prior.HERE/'inventory',root/'inventory')
        shutil.copyfile(verify_prior.HERE/'DESIGN-080-hashes.json',root/'DESIGN-080-hashes.json')
    def test_changed_or_missing_prior_receipt_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=pathlib.Path(tmp);self.temp_copy(root); target=root/'prior-receipts/r10-019.json';target.write_bytes(target.read_bytes()+b' ')
            with patch.object(verify_prior,'HERE',root), self.assertRaisesRegex(ValueError,'hash mismatch'): verify_prior.verify_prior_receipts()
            target.unlink()
            with patch.object(verify_prior,'HERE',root), self.assertRaises(FileNotFoundError): verify_prior.verify_prior_receipts()
    def test_symlinked_prior_receipt_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=pathlib.Path(tmp);self.temp_copy(root);target=root/'prior-receipts/r10-019.json';target.unlink();target.symlink_to(root/'untrusted');(root/'untrusted').write_text('synthetic')
            with patch.object(verify_prior,'HERE',root), self.assertRaises(OSError): verify_prior.verify_prior_receipts()
    def test_tampered_index_fails_before_receipt_use(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=pathlib.Path(tmp);self.temp_copy(root);p=root/'DESIGN-080-hashes.json';p.write_bytes(p.read_bytes()+b' ')
            with patch.object(verify_prior,'HERE',root), self.assertRaisesRegex(ValueError,'index hash'): verify_prior.verify_prior_receipts()
    def test_unknown_historical_schema_is_rejected_even_if_rehashed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=pathlib.Path(tmp);self.temp_copy(root);p=root/'prior-receipts/r10-028.json'
            receipt=json.loads(p.read_bytes());receipt.pop('incomplete_reasons');raw=json.dumps(receipt).encode();p.write_bytes(raw)
            idx=root/'DESIGN-080-hashes.json';data=json.loads(idx.read_bytes())
            next(x for x in data['prior_receipts'] if x['side']=='r10' and x['group']=='028')['sha256']=hashlib.sha256(raw).hexdigest()
            encoded=json.dumps(data).encode();idx.write_bytes(encoded)
            with patch.object(verify_prior,'HERE',root), patch.object(verify_prior,'PINNED_INDEX_SHA256',hashlib.sha256(encoded).hexdigest()), self.assertRaisesRegex(ValueError,'historical schema'):
                verify_prior.verify_prior_receipts()
    def test_duplicate_planned_range_is_rejected(self):
        altered=dict(verify_prior.SIDE_RANGES);altered['r10']=altered['r10']+('041-041',)
        with patch.object(verify_prior,'SIDE_RANGES',altered), self.assertRaisesRegex(ValueError,'remaining coverage'):
            verify_prior.verify_prior_receipts()
if __name__=='__main__': unittest.main()
