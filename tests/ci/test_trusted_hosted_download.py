import hashlib
import io
from pathlib import Path
import sys
import tempfile
import unittest
import zipfile


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts/ci"))
import trusted_hosted_download as hosted


class TrustedHostedDownloadTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.sha = "a" * 40
        self.repos = {"agent": {"url": "https://github.com/banodoco/astrid-agent", "sha": self.sha}}
        self.source = {"kind": "hosted", "repo": "agent", "workflow_path": ".github/workflows/synthetic.yml", "workflow_revision": "b" * 40,
                       "run_id": 11, "run_attempt": 2, "job_id": 12, "artifact_id": 13, "artifact_name": "agent-linux", "head_sha": self.sha}
        self.archive = self.zip_bytes({"manifest.json": b"{}", "junit.xml": b"<testsuite><testcase/></testsuite>"})

    @staticmethod
    def zip_bytes(entries):
        output = io.BytesIO()
        with zipfile.ZipFile(output, "w") as archive:
            for name, content in entries.items():
                archive.writestr(name, content)
        return output.getvalue()

    def fetch(self, path, binary=False):
        base = "/repos/banodoco/astrid-agent/actions"
        responses = {
            f"{base}/runs/11": {"id": 11, "run_attempt": 2, "head_sha": self.sha, "status": "completed", "conclusion": "success",
                                  "path": self.source["workflow_path"], "repository": {"html_url": self.repos["agent"]["url"]}},
            f"{base}/runs/11/attempts/2/jobs?per_page=100": {"total_count": 1, "jobs": [{"id": 12, "run_id": 11, "run_attempt": 2, "status": "completed", "conclusion": "success"}]},
            f"{base}/runs/11/artifacts?per_page=100": {"total_count": 1, "artifacts": [{"id": 13, "name": "agent-linux", "expired": False,
                                                                                    "workflow_run": {"id": 11}, "digest": "sha256:" + hashlib.sha256(self.archive).hexdigest()}]},
            f"{base}/artifacts/13/zip": self.archive,
        }
        return responses[path]

    def test_authenticated_download_binds_all_source_identities(self):
        receipt = hosted.download(self.source, self.repos, self.root / "artifact", self.fetch)
        self.assertEqual(receipt["run"]["head_sha"], self.sha)
        self.assertEqual({entry["path"] for entry in receipt["files"]}, {"manifest.json", "junit.xml"})

    def test_wrong_tuple_digest_wrong_run_and_archive_digest_are_rejected(self):
        wrong = dict(self.source, head_sha="f" * 40)
        with self.assertRaisesRegex(ValueError, "tuple-bound"):
            hosted.download(wrong, self.repos, self.root / "wrong-tuple", self.fetch)
        def bad_run(path, binary=False):
            result = self.fetch(path, binary)
            if path.endswith("/runs/11"):
                result = dict(result, head_sha="f" * 40)
            return result
        with self.assertRaisesRegex(ValueError, "wrong or unsuccessful"):
            hosted.download(self.source, self.repos, self.root / "wrong-run", bad_run)
        def bad_archive(path, binary=False):
            return b"bad" if path.endswith("/zip") else self.fetch(path, binary)
        with self.assertRaisesRegex(ValueError, "digest"):
            hosted.download(self.source, self.repos, self.root / "wrong-archive", bad_archive)

    def test_unsafe_and_duplicate_archive_members_are_rejected(self):
        self.archive = self.zip_bytes({"manifest.json": b"{}", "junit.xml": b"<testsuite/>", "../escape": b"x"})
        with self.assertRaisesRegex(ValueError, "unsafe"):
            hosted.download(self.source, self.repos, self.root / "unsafe", self.fetch)
        output = io.BytesIO()
        with zipfile.ZipFile(output, "w") as archive:
            archive.writestr("manifest.json", b"{}")
            archive.writestr("manifest.json", b"again")
            archive.writestr("junit.xml", b"<testsuite/>")
        self.archive = output.getvalue()
        with self.assertRaisesRegex(ValueError, "duplicate"):
            hosted.download(self.source, self.repos, self.root / "duplicate", self.fetch)


if __name__ == "__main__":
    unittest.main()
