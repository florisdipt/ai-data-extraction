#!/usr/bin/env python3
"""Extract supported AI coding sessions into an append-only local archive."""

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows is not a supported scheduler.
    fcntl = None


SUPPORTED_EXTRACTORS = (
    ("claude-code", "extract_claude_code.py"),
    ("codex", "extract_codex.py"),
    ("cursor", "extract_cursor.py"),
    ("trae", "extract_trae.py"),
    ("windsurf", "extract_windsurf.py"),
    ("continue", "extract_continue.py"),
    ("gemini", "extract_gemini.py"),
    ("opencode", "extract_opencode.py"),
)


def utc_now():
    return dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def default_data_dir():
    configured = os.environ.get("AI_DATA_EXTRACTION_DATA_DIR")
    if configured:
        return Path(configured).expanduser()
    data_home = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
    return data_home / "ai-data-extraction"


def default_host_id():
    return os.environ.get("AI_DATA_EXTRACTION_HOST_ID", socket.gethostname())


def safe_component(value, fallback="unknown"):
    text = str(value or "").strip().lower()
    text = re.sub(r"[^a-z0-9._-]+", "-", text)
    text = text.strip(".-")
    return text[:120] or fallback


def sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()


def sha256_file(path):
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def canonical_json(value):
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def atomic_write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(".%s.%s.tmp" % (path.name, os.getpid()))
    with temporary.open("wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(str(temporary), str(path))


def atomic_copy(source, destination, expected_sha256=None):
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        if expected_sha256 and sha256_file(destination) != expected_sha256:
            raise RuntimeError("existing archive object has a different hash: %s" % destination)
        return False

    temporary = destination.with_name(".%s.%s.tmp" % (destination.name, os.getpid()))
    shutil.copyfile(str(source), str(temporary))
    if expected_sha256 and sha256_file(temporary) != expected_sha256:
        if temporary.exists():
            temporary.unlink()
        raise RuntimeError("source copy hash changed while reading: %s" % source)
    os.replace(str(temporary), str(destination))
    return True


def acquire_lock(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+")
    if fcntl is None:
        return handle
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        handle.close()
        return None
    return handle


def initialize_state(database):
    database.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(str(database))
    connection.execute("PRAGMA journal_mode=WAL")
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS runs (
            run_id TEXT PRIMARY KEY,
            host_id TEXT NOT NULL,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            status TEXT NOT NULL,
            manifest_path TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS raw_outputs (
            source TEXT NOT NULL,
            sha256 TEXT NOT NULL,
            archive_path TEXT NOT NULL,
            first_seen TEXT NOT NULL,
            PRIMARY KEY (source, sha256)
        );
        CREATE TABLE IF NOT EXISTS snapshots (
            source TEXT NOT NULL,
            session_key TEXT NOT NULL,
            sha256 TEXT NOT NULL,
            archive_path TEXT NOT NULL,
            first_seen TEXT NOT NULL,
            last_seen TEXT NOT NULL,
            PRIMARY KEY (source, session_key, sha256)
        );
        CREATE INDEX IF NOT EXISTS snapshots_session_idx
            ON snapshots (source, session_key, last_seen);
        """
    )
    connection.commit()
    return connection


def git_revision(repo_root):
    try:
        result = subprocess.run(
            ["git", "-C", str(repo_root), "rev-parse", "HEAD"],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        )
    except OSError:
        return None
    return result.stdout.strip() if result.returncode == 0 else None


def identity_value(conversation, source):
    identity_fields = (
        "session_id",
        "conversation_id",
        "composer_id",
        "composerId",
        "tab_id",
        "tabId",
        "chat_id",
        "chatId",
    )
    for field in identity_fields:
        value = conversation.get(field)
        if value not in (None, "", [], {}):
            return "%s:%s" % (field, value)

    context_fields = (
        "source_file",
        "project_path",
        "project_name",
        "installation",
        "name",
        "created_at",
        "createdAt",
    )
    context = [str(conversation.get(field)) for field in context_fields if conversation.get(field)]
    if context:
        return "context:%s" % "|".join(context)
    return "content:%s" % sha256_bytes(canonical_json(conversation))


def session_key(conversation, source):
    identity = "%s|%s" % (source, identity_value(conversation, source))
    return "s-%s" % hashlib.sha256(identity.encode("utf-8")).hexdigest()[:40]


def read_jsonl(path):
    records = []
    errors = []
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError as error:
                errors.append("line %d: %s" % (line_number, error))
                continue
            if not isinstance(value, dict):
                errors.append("line %d: output is not a JSON object" % line_number)
                continue
            records.append(value)
    return records, errors


def process_output(source, output_file, archive_root, connection, seen_at):
    raw_sha256 = sha256_file(output_file)
    raw_path = archive_root / "raw" / safe_component(source) / (raw_sha256 + ".jsonl")
    raw_created = atomic_copy(output_file, raw_path, raw_sha256)
    connection.execute(
        "INSERT OR IGNORE INTO raw_outputs(source, sha256, archive_path, first_seen) VALUES (?, ?, ?, ?)",
        (source, raw_sha256, str(raw_path), seen_at),
    )

    conversations, parse_errors = read_jsonl(output_file)
    new_snapshots = 0
    existing_snapshots = 0
    snapshot_paths = []
    for conversation in conversations:
        payload = canonical_json(conversation) + b"\n"
        content_sha256 = sha256_bytes(canonical_json(conversation))
        key = session_key(conversation, source)
        snapshot_path = (
            archive_root
            / "sessions"
            / safe_component(source)
            / key
            / (content_sha256 + ".jsonl")
        )
        created = atomic_write_if_missing(snapshot_path, payload, content_sha256)
        if created:
            new_snapshots += 1
        else:
            existing_snapshots += 1
        connection.execute(
            """
            INSERT INTO snapshots(source, session_key, sha256, archive_path, first_seen, last_seen)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(source, session_key, sha256)
            DO UPDATE SET last_seen=excluded.last_seen
            """,
            (source, key, content_sha256, str(snapshot_path), seen_at, seen_at),
        )
        snapshot_paths.append(str(snapshot_path))

    connection.commit()
    return {
        "source": source,
        "output_file": str(output_file),
        "raw_sha256": raw_sha256,
        "raw_archive_path": str(raw_path),
        "raw_created": raw_created,
        "conversation_count": len(conversations),
        "parse_errors": parse_errors,
        "new_snapshots": new_snapshots,
        "existing_snapshots": existing_snapshots,
        "snapshot_paths": snapshot_paths,
    }


def atomic_write_if_missing(path, data, content_sha256):
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        existing = path.read_bytes()
        existing_payload = existing[:-1] if existing.endswith(b"\n") else existing
        if sha256_bytes(existing_payload) != content_sha256:
            raise RuntimeError("existing session snapshot has a different hash: %s" % path)
        return False
    atomic_write(path, data)
    return True


def run_extractor(
    repo_root,
    source,
    script_name,
    run_logs,
    archive_root,
    connection,
    run_id,
    timeout,
):
    script_path = repo_root / script_name
    log_path = run_logs / (safe_component(source) + ".log")
    result = {
        "source": source,
        "script": script_name,
        "script_exists": script_path.exists(),
        "status": "not_run",
        "returncode": None,
        "log_path": str(log_path),
        "outputs": [],
        "new_snapshots": 0,
        "existing_snapshots": 0,
        "errors": [],
    }
    if not script_path.exists():
        result["status"] = "missing_script"
        result["errors"].append("script does not exist")
        return result

    run_logs.mkdir(parents=True, exist_ok=True)
    stage_dir = Path(tempfile.mkdtemp(prefix="ai-data-extraction-%s-" % safe_component(source)))
    try:
        with log_path.open("w", encoding="utf-8") as log_handle:
            try:
                completed = subprocess.run(
                    [sys.executable, str(script_path)],
                    cwd=str(stage_dir),
                    stdout=log_handle,
                    stderr=subprocess.STDOUT,
                    check=False,
                    timeout=timeout,
                )
                result["returncode"] = completed.returncode
            except subprocess.TimeoutExpired:
                result["status"] = "timeout"
                result["errors"].append("extractor exceeded %s seconds" % timeout)
                return result
            except OSError as error:
                result["status"] = "error"
                result["errors"].append(str(error))
                return result

        output_files = sorted((stage_dir / "extracted_data").glob("*.jsonl"))
        result["output_files"] = [str(path) for path in output_files]
        if not output_files:
            result["status"] = "error" if result["returncode"] else "no_data"
            if result["returncode"]:
                result["errors"].append("extractor returned %s" % result["returncode"])
            return result

        result["status"] = "complete" if result["returncode"] == 0 else "partial"
        for output_file in output_files:
            try:
                output_result = process_output(
                    source,
                    output_file,
                    archive_root,
                    connection,
                    utc_now(),
                )
                result["outputs"].append(output_result)
                result["new_snapshots"] += output_result["new_snapshots"]
                result["existing_snapshots"] += output_result["existing_snapshots"]
                result["errors"].extend(output_result["parse_errors"])
            except Exception as error:
                result["status"] = "error"
                result["errors"].append(str(error))
        return result
    finally:
        shutil.rmtree(str(stage_dir), ignore_errors=True)


def write_manifest(path, manifest):
    atomic_write(path, json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8") + b"\n")


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=Path(__file__).resolve().parent)
    parser.add_argument("--data-dir", type=Path, default=default_data_dir())
    parser.add_argument("--host-id", default=default_host_id())
    parser.add_argument("--timeout", type=int, default=3600)
    parser.add_argument(
        "--source",
        dest="sources",
        action="append",
        choices=[name for name, _ in SUPPORTED_EXTRACTORS],
        help="Run one source. Repeat the option to select several sources.",
    )
    return parser.parse_args(argv)


def main(argv=None):
    os.umask(0o077)
    args = parse_args(argv)
    repo_root = args.repo_root.expanduser().resolve()
    data_dir = args.data_dir.expanduser().resolve()
    archive_root = data_dir / "archive"
    state_dir = data_dir / "state"
    lock_handle = acquire_lock(state_dir / "extract.lock")
    if lock_handle is None:
        print("extraction already runs")
        return 75

    run_id = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ") + "-" + uuid.uuid4().hex[:8]
    run_dir = archive_root / "runs" / run_id
    run_logs = run_dir / "logs"
    manifest_path = run_dir / "manifest.json"
    started_at = utc_now()
    selected = set(args.sources or [name for name, _ in SUPPORTED_EXTRACTORS])
    connection = initialize_state(state_dir / "state.sqlite3")
    connection.execute(
        "INSERT INTO runs(run_id, host_id, started_at, status, manifest_path) VALUES (?, ?, ?, ?, ?)",
        (run_id, args.host_id, started_at, "running", str(manifest_path)),
    )
    connection.commit()

    results = []
    try:
        for source, script_name in SUPPORTED_EXTRACTORS:
            if source not in selected:
                continue
            print("extracting %s" % source)
            result = run_extractor(
                repo_root,
                source,
                script_name,
                run_logs,
                archive_root,
                connection,
                run_id,
                args.timeout,
            )
            results.append(result)
            print(
                "%s: %s, %s new snapshots"
                % (source, result["status"], result["new_snapshots"])
            )

        failed = [
            result
            for result in results
            if result["status"] in ("error", "timeout", "missing_script", "partial")
        ]
        finished_at = utc_now()
        status = "partial" if failed else "complete"
        manifest = {
            "schema_version": 1,
            "run_id": run_id,
            "host_id": args.host_id,
            "repo_root": str(repo_root),
            "repo_revision": git_revision(repo_root),
            "started_at": started_at,
            "finished_at": finished_at,
            "status": status,
            "extractors": results,
        }
        write_manifest(manifest_path, manifest)
        connection.execute(
            "UPDATE runs SET finished_at=?, status=? WHERE run_id=?",
            (finished_at, status, run_id),
        )
        connection.commit()
        print("run manifest: %s" % manifest_path)
        return 1 if failed else 0
    finally:
        connection.close()
        if fcntl is not None:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)
        lock_handle.close()


if __name__ == "__main__":
    sys.exit(main())
