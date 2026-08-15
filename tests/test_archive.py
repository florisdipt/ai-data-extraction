import json
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

import extract_incremental
import upload_all_to_s3_compatible


class IncrementalExtractionTests(unittest.TestCase):
    def test_unchanged_runs_do_not_duplicate_and_changed_runs_are_kept(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            script = root / "extract_fake.py"
            script.write_text(
                "import json, os\n"
                "from pathlib import Path\n"
                "Path('extracted_data').mkdir()\n"
                "value = os.environ.get('AI_DATA_EXTRACTION_TEST_VALUE', 'one')\n"
                "record = {'session_id': 'session-1', 'messages': [{'role': 'user', 'content': value}]}\n"
                "Path('extracted_data/fake.jsonl').write_text(json.dumps(record) + '\\n')\n",
                encoding="utf-8",
            )
            data_dir = root / "data"
            old_extractors = extract_incremental.SUPPORTED_EXTRACTORS
            try:
                extract_incremental.SUPPORTED_EXTRACTORS = (("fake", "extract_fake.py"),)
                with mock.patch.dict(os.environ, {"AI_DATA_EXTRACTION_TEST_VALUE": "one"}):
                    self.assertEqual(
                        extract_incremental.main(
                            ["--repo-root", str(root), "--data-dir", str(data_dir), "--host-id", "test-host"]
                        ),
                        0,
                    )
                with mock.patch.dict(os.environ, {"AI_DATA_EXTRACTION_TEST_VALUE": "one"}):
                    self.assertEqual(
                        extract_incremental.main(
                            ["--repo-root", str(root), "--data-dir", str(data_dir), "--host-id", "test-host"]
                        ),
                        0,
                    )
                with mock.patch.dict(os.environ, {"AI_DATA_EXTRACTION_TEST_VALUE": "two"}):
                    self.assertEqual(
                        extract_incremental.main(
                            ["--repo-root", str(root), "--data-dir", str(data_dir), "--host-id", "test-host"]
                        ),
                        0,
                    )
            finally:
                extract_incremental.SUPPORTED_EXTRACTORS = old_extractors

            snapshots = list((data_dir / "archive" / "sessions" / "fake").glob("*/*.jsonl"))
            self.assertEqual(len(snapshots), 2)
            manifests = list((data_dir / "archive" / "runs").glob("*/manifest.json"))
            self.assertEqual(len(manifests), 3)
            self.assertTrue(all(json.loads(path.read_text(encoding="utf-8"))["status"] == "complete" for path in manifests))


class MissingObject(Exception):
    def __init__(self):
        super().__init__("missing")
        self.response = {"Error": {"Code": "404"}}


class FakeS3Client:
    def __init__(self):
        self.objects = {}

    def head_object(self, Bucket, Key):
        try:
            return self.objects[(Bucket, Key)]
        except KeyError:
            raise MissingObject()

    def put_local(self, Bucket, Key, path, digest):
        self.objects[(Bucket, Key)] = {
            "ContentLength": path.stat().st_size,
            "Metadata": {"sha256": digest},
        }


class UploadTests(unittest.TestCase):
    def test_upload_skips_verified_objects_and_never_needs_delete(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            archive = root / "data" / "archive" / "sessions" / "fake" / "session"
            archive.mkdir(parents=True)
            object_path = archive / "hash.jsonl"
            object_path.write_bytes(b'{"messages": []}\n')
            config_path = root / "config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "endpoint_url": "https://example.invalid",
                        "bucket": "ai-data-extraction",
                        "region": "global",
                        "access_key_id": "test",
                        "secret_access_key": "test",
                        "host_id": "test-host",
                        "data_dir": str(root / "data"),
                    }
                ),
                encoding="utf-8",
            )
            config_path.chmod(stat.S_IRUSR | stat.S_IWUSR)
            client = FakeS3Client()

            def fake_upload(client_arg, bucket, key, path, digest, verify=True):
                client_arg.put_local(bucket, key, path, digest)

            with mock.patch.object(upload_all_to_s3_compatible, "build_client", return_value=client):
                with mock.patch.object(upload_all_to_s3_compatible, "upload_one", side_effect=fake_upload):
                    self.assertEqual(upload_all_to_s3_compatible.main(["--config", str(config_path)]), 0)
                    self.assertEqual(upload_all_to_s3_compatible.main(["--config", str(config_path)]), 0)

            self.assertEqual(len(client.objects), 1)

    def test_environment_path_and_host_override_config_defaults(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            data_dir = root / "native-data"
            archive = data_dir / "archive" / "sessions" / "fake" / "session"
            archive.mkdir(parents=True)
            (archive / "hash.jsonl").write_bytes(b'{"messages": []}\n')
            config_path = root / "config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "endpoint_url": "https://example.invalid",
                        "bucket": "ai-data-extraction",
                        "region": "global",
                        "access_key_id": "test",
                        "secret_access_key": "test",
                        "host_id": "config-host",
                        "data_dir": str(root / "wrong-data"),
                    }
                ),
                encoding="utf-8",
            )
            config_path.chmod(stat.S_IRUSR | stat.S_IWUSR)
            client = FakeS3Client()

            def fake_upload(client_arg, bucket, key, path, digest, verify=True):
                client_arg.put_local(bucket, key, path, digest)

            with mock.patch.dict(
                os.environ,
                {
                    "AI_DATA_EXTRACTION_DATA_DIR": str(data_dir),
                    "AI_DATA_EXTRACTION_HOST_ID": "macos-test-host",
                },
            ):
                with mock.patch.object(upload_all_to_s3_compatible, "build_client", return_value=client):
                    with mock.patch.object(upload_all_to_s3_compatible, "upload_one", side_effect=fake_upload):
                        self.assertEqual(upload_all_to_s3_compatible.main(["--config", str(config_path)]), 0)

            self.assertIn(
                ("ai-data-extraction", "hosts/macos-test-host/sessions/fake/session/hash.jsonl"),
                client.objects,
            )


if __name__ == "__main__":
    unittest.main()
