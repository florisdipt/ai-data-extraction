# Operations

## Object layout

The uploader stores one host below one bucket prefix:

```text
s3://ai-data-extraction/hosts/<host-id>/sessions/...
s3://ai-data-extraction/hosts/<host-id>/raw/...
s3://ai-data-extraction/hosts/<host-id>/runs/...
```

The object key includes the content hash for session snapshots and raw output.
An updated session creates a new object. The old object stays in the bucket.

## Configuration

Store the configuration at:

```text
~/.config/ai-data-extraction/config.json
```

Set the file mode to `0600`. Do not copy its secret values into a project
record, log, Git commit, or chat message.

The configuration uses these fields:

| Field | Required | Meaning |
|---|---:|---|
| `endpoint_url` | yes | S3-compatible HTTPS endpoint |
| `bucket` | yes | Dedicated archive bucket |
| `region` | yes | S3 signing region |
| `access_key_id` | yes | Scoped service-account key |
| `secret_access_key` | yes | Scoped service-account secret |
| `host_id` | yes | Stable name for this Linux or macOS source |
| `data_dir` | no | Local archive directory |

## Linux timer

The systemd timer runs `scripts/run_backup_once.sh` once per hour. The service
uses a lock so two archive runs cannot overlap. A root install creates a
system timer; a non-root install creates a user timer.

When systemd is not available, `scripts/install_linux.sh` installs a guarded
shell hook. The hook starts one supervisor when an interactive shell starts.
The supervisor waits for the next UTC hour and runs the same command.

## macOS command

Use `scripts/run_macos_once.sh` for a manual native macOS run. The command does
not install a launchd job or change the macOS scheduler.

## Recovery

The uploader never calls an object-delete operation. If a run stops during an
upload, run the same command again. Existing verified objects are skipped and
missing objects upload on the next run.

To restore one object, download it from its host prefix and compare its
SHA-256 value with the object metadata and the local run manifest.
