#!/usr/bin/env python3
"""Upload the local append-only archive to an S3-compatible bucket."""

import argparse
import json
import mimetypes
import os
import re
import socket
import stat
import sys
from pathlib import Path
from urllib.parse import urlparse


def default_data_dir():
    configured = os.environ.get("AI_DATA_EXTRACTION_DATA_DIR")
    if configured:
        return Path(configured).expanduser()
    data_home = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
    return data_home / "ai-data-extraction"


def default_config_candidates():
    candidates = []
    configured = os.environ.get("AI_DATA_EXTRACTION_CONFIG")
    if configured:
        candidates.append(Path(configured).expanduser())
    candidates.append(
        Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
        / "ai-data-extraction"
        / "config.json"
    )
    candidates.append(Path.home() / "Library" / "Application Support" / "ai-data-extraction" / "config.json")
    return candidates


def load_config(path):
    if not path.exists():
        raise RuntimeError("S3 configuration does not exist: %s" % path)
    mode = stat.S_IMODE(path.stat().st_mode)
    if mode & 0o077:
        raise RuntimeError("S3 configuration must be private, mode 0600: %s" % path)
    with path.open("r", encoding="utf-8") as handle:
        config = json.load(handle)
    if not isinstance(config, dict):
        raise RuntimeError("S3 configuration must contain a JSON object")
    return config


def config_value(config, *names, default=None):
    for name in names:
        value = config.get(name)
        if value not in (None, ""):
            return value
    return default


def sha256_file(path):
    import hashlib

    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def safe_component(value):
    text = re.sub(r"[^A-Za-z0-9._-]+", "-", str(value).strip())
    return text.strip(".-") or "unknown-host"


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, help="Private JSON configuration file")
    parser.add_argument("--data-dir", type=Path)
    parser.add_argument("--host-id")
    parser.add_argument("--prefix", default="hosts")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--verify-only", action="store_true")
    parser.add_argument("--verbose", action="store_true", help="print every object key")
    return parser.parse_args(argv)


def find_config(explicit):
    if explicit:
        return explicit.expanduser().resolve()
    for candidate in default_config_candidates():
        candidate = candidate.expanduser().resolve()
        if candidate.exists():
            return candidate
    raise RuntimeError("no S3 configuration found")


def endpoint_parts(endpoint):
    parsed = urlparse(str(endpoint))
    if parsed.scheme:
        if parsed.scheme not in ("http", "https") or not parsed.netloc or parsed.path not in ("", "/"):
            raise RuntimeError("S3 endpoint must be an HTTP(S) host URL")
        return parsed.netloc, parsed.scheme == "https"
    endpoint = str(endpoint).rstrip("/")
    if not endpoint:
        raise RuntimeError("S3 endpoint is required")
    return endpoint, True


def build_client(config):
    try:
        import urllib3
        from minio import Minio
    except ImportError as error:
        raise RuntimeError("minio is required. Install requirements.txt") from error

    endpoint_url = config_value(config, "endpoint_url", "endpoint", default=os.environ.get("S3_ENDPOINT_URL"))
    region_name = config_value(config, "region", default=os.environ.get("AWS_REGION", "global"))
    access_key_id = config_value(
        config,
        "access_key_id",
        "aws_access_key_id",
        "accessKey",
        default=os.environ.get("AWS_ACCESS_KEY_ID"),
    )
    secret_access_key = config_value(
        config,
        "secret_access_key",
        "aws_secret_access_key",
        "secretKey",
        default=os.environ.get("AWS_SECRET_ACCESS_KEY"),
    )
    if not endpoint_url or not access_key_id or not secret_access_key:
        raise RuntimeError("S3 endpoint and credentials are required")

    endpoint, secure = endpoint_parts(endpoint_url)
    http_client = urllib3.PoolManager(
        timeout=urllib3.Timeout(connect=5.0, read=30.0),
        retries=urllib3.Retry(
            total=3,
            connect=3,
            read=3,
            status=3,
            redirect=0,
            backoff_factor=0.2,
            status_forcelist=(500, 502, 503, 504),
            allowed_methods=frozenset(("GET", "PUT", "POST")),
            raise_on_status=False,
        ),
    )
    return Minio(
        endpoint,
        access_key=access_key_id,
        secret_key=secret_access_key,
        secure=secure,
        region=region_name,
        http_client=http_client,
    )


def remote_sha256(metadata):
    for key, value in (metadata or {}).items():
        normalized = str(key).lower()
        if normalized.startswith("x-amz-meta-"):
            normalized = normalized[len("x-amz-meta-") :]
        if normalized == "sha256":
            return str(value)
    return None


def archive_files(data_dir):
    archive_root = data_dir / "archive"
    if not archive_root.exists():
        return []
    return sorted(path for path in archive_root.rglob("*") if path.is_file() and not path.name.endswith(".tmp"))


def host_object_prefix(prefix, host_id):
    clean_prefix = prefix.strip("/")
    host = safe_component(host_id)
    return "%s/%s/" % (clean_prefix, host) if clean_prefix else "%s/" % host


def object_key(archive_root, path, prefix, host_id):
    relative = path.relative_to(archive_root).as_posix()
    return host_object_prefix(prefix, host_id) + relative


def remote_inventory(client, bucket, prefix, host_id):
    inventory = {}
    object_prefix = host_object_prefix(prefix, host_id)
    for remote in client.list_objects(
        bucket,
        prefix=object_prefix,
        recursive=True,
        include_user_meta=True,
    ):
        inventory[remote.object_name] = remote
    return inventory


def remote_matches(remote, size, digest):
    return (
        remote is not None
        and int(getattr(remote, "size", -1)) == size
        and remote_sha256(getattr(remote, "metadata", None)) == digest
    )


def upload_one(client, bucket, key, path, digest):
    content_type = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
    client.fput_object(
        bucket,
        key,
        str(path),
        content_type=content_type,
        metadata={"sha256": digest, "source-size": str(path.stat().st_size)},
        part_size=8 * 1024 * 1024,
        num_parallel_uploads=4,
    )


def main(argv=None):
    os.umask(0o077)
    args = parse_args(argv)
    config_path = find_config(args.config)
    config = load_config(config_path)
    configured_data_dir = config_value(config, "data_dir")
    environment_data_dir = os.environ.get("AI_DATA_EXTRACTION_DATA_DIR")
    data_dir = (args.data_dir or Path(environment_data_dir or configured_data_dir or default_data_dir())).expanduser().resolve()
    configured_host_id = config_value(config, "host_id")
    environment_host_id = os.environ.get("AI_DATA_EXTRACTION_HOST_ID")
    host_id = args.host_id or environment_host_id or configured_host_id or socket.gethostname()
    bucket = config_value(config, "bucket", default=os.environ.get("S3_BUCKET", "ai-data-extraction"))
    archive_root = data_dir / "archive"
    files = archive_files(data_dir)
    if not files:
        print("no archive files found under %s" % archive_root)
        return 0

    client = None if args.dry_run else build_client(config)
    inventory = {} if args.dry_run else remote_inventory(client, bucket, args.prefix, str(host_id))
    uploaded = 0
    skipped = 0
    verified = 0
    for path in files:
        digest = sha256_file(path)
        key = object_key(archive_root, path, args.prefix, str(host_id))
        if args.dry_run:
            print("would upload s3://%s/%s (%d bytes)" % (bucket, key, path.stat().st_size))
            continue

        remote = inventory.get(key)
        if remote is not None:
            if not remote_matches(remote, path.stat().st_size, digest):
                raise RuntimeError("remote object exists with different or missing metadata: s3://%s/%s" % (bucket, key))
            skipped += 1
            if args.verify_only:
                verified += 1
            continue
        if args.verify_only:
            raise RuntimeError("remote object is missing: s3://%s/%s" % (bucket, key))

        if args.verbose:
            print("uploading s3://%s/%s" % (bucket, key))
        upload_one(client, bucket, key, path, digest)
        uploaded += 1

    print("uploaded=%d skipped=%d verified=%d files=%d" % (uploaded, skipped, verified, len(files)))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as error:
        print("upload failed: %s" % error, file=sys.stderr)
        sys.exit(1)
