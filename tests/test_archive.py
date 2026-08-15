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


class FakeMinioObject:
    def __init__(self, object_name, size, metadata):
        self.object_name = object_name
        self.size = size
        self.metadata = metadata


class FakeMinioClient:
    def __init__(self):
        self.objects = {}
        self.list_calls = 0

    def list_objects(self, bucket_name, prefix=None, recursive=False, include_user_meta=False):
        self.list_calls += 1
        for (bucket, key), remote in sorted(self.objects.items()):
            if bucket == bucket_name and (prefix is None or key.startswith(prefix)):
                yield remote

    def fput_object(self, bucket_name, object_name, file_path, **kwargs):
        self.objects[(bucket_name, object_name)] = FakeMinioObject(
            object_name,
            Path(file_path).stat().st_size,
            kwargs["metadata"],
        )


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
            client = FakeMinioClient()

            with mock.patch.object(upload_all_to_s3_compatible, "build_client", return_value=client):
                self.assertEqual(upload_all_to_s3_compatible.main(["--config", str(config_path)]), 0)
                self.assertEqual(upload_all_to_s3_compatible.main(["--config", str(config_path)]), 0)

            self.assertEqual(len(client.objects), 1)
            self.assertEqual(client.list_calls, 2)

    def test_mismatched_remote_object_fails_without_overwrite(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            data_dir = root / "data"
            archive = data_dir / "archive" / "sessions" / "fake" / "session"
            archive.mkdir(parents=True)
            object_path = archive / "hash.jsonl"
            object_path.write_bytes(b'{"messages": ["new"]}\n')
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
                        "data_dir": str(data_dir),
                    }
                ),
                encoding="utf-8",
            )
            config_path.chmod(stat.S_IRUSR | stat.S_IWUSR)
            client = FakeMinioClient()
            key = "hosts/test-host/sessions/fake/session/hash.jsonl"
            client.objects[("ai-data-extraction", key)] = FakeMinioObject(
                key,
                object_path.stat().st_size,
                {"sha256": "different"},
            )

            with mock.patch.object(upload_all_to_s3_compatible, "build_client", return_value=client):
                with self.assertRaisesRegex(RuntimeError, "different or missing metadata"):
                    upload_all_to_s3_compatible.main(["--config", str(config_path)])

            self.assertEqual(client.objects[("ai-data-extraction", key)].metadata["sha256"], "different")

    def test_verify_only_uses_inventory_without_uploading(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            data_dir = root / "data"
            archive = data_dir / "archive" / "sessions" / "fake" / "session"
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
                        "data_dir": str(data_dir),
                    }
                ),
                encoding="utf-8",
            )
            config_path.chmod(stat.S_IRUSR | stat.S_IWUSR)
            client = FakeMinioClient()
            digest = upload_all_to_s3_compatible.sha256_file(object_path)
            key = "hosts/test-host/sessions/fake/session/hash.jsonl"
            client.objects[("ai-data-extraction", key)] = FakeMinioObject(
                key,
                object_path.stat().st_size,
                {"sha256": digest},
            )

            with mock.patch.object(upload_all_to_s3_compatible, "build_client", return_value=client):
                self.assertEqual(
                    upload_all_to_s3_compatible.main(["--config", str(config_path), "--verify-only"]),
                    0,
                )

            self.assertEqual(client.list_calls, 1)

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
            client = FakeMinioClient()

            with mock.patch.dict(
                os.environ,
                {
                    "AI_DATA_EXTRACTION_DATA_DIR": str(data_dir),
                    "AI_DATA_EXTRACTION_HOST_ID": "macos-test-host",
                },
            ):
                with mock.patch.object(upload_all_to_s3_compatible, "build_client", return_value=client):
                    self.assertEqual(upload_all_to_s3_compatible.main(["--config", str(config_path)]), 0)

            self.assertIn(
                ("ai-data-extraction", "hosts/macos-test-host/sessions/fake/session/hash.jsonl"),
                client.objects,
            )


if __name__ == "__main__":
    unittest.main()
