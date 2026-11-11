import pathlib, shutil, tempfile, unittest
from unittest.mock import patch
import verify_prior

class PriorReceiptTests(unittest.TestCase):
    def test_five_committed_receipts_and_ids_validate(self):
        self.assertTrue(verify_prior.verify_prior_receipts())
        self.assertEqual(len(verify_prior.PRIOR),5)
    def test_changed_or_missing_receipt_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=pathlib.Path(tmp); shutil.copytree(verify_prior.HERE/'prior-receipts',root/'prior-receipts')
            target=root/'prior-receipts/r10-019.json'; target.write_bytes(target.read_bytes()+b' ')
            with patch.object(verify_prior,'HERE',root), self.assertRaisesRegex(ValueError,'hash mismatch'):
                verify_prior.verify_prior_receipts()
            target.unlink()
            with patch.object(verify_prior,'HERE',root), self.assertRaises(FileNotFoundError):
                verify_prior.verify_prior_receipts()
    def test_symlinked_prior_receipt_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=pathlib.Path(tmp); shutil.copytree(verify_prior.HERE/'prior-receipts',root/'prior-receipts')
            target=root/'prior-receipts/r10-019.json'; target.unlink(); target.symlink_to(root/'untrusted')
            (root/'untrusted').write_text('synthetic')
            with patch.object(verify_prior,'HERE',root), self.assertRaises(OSError): verify_prior.verify_prior_receipts()
if __name__=='__main__': unittest.main()
