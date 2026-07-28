# backup-to-r2

Daily backup script: safely snapshots in-use SQLite databases, then syncs the entire directory to Cloudflare R2 via rclone.

## Requirements

| Tool | 
|---|
| `sqlite3` |
| `rclone` |

## Setup

### 1. Configure rclone

```bash
rclone config
```

Add a remote (e.g. `r2`) with S3-compatible Cloudflare endpoint:

```
[r2]
type = s3
provider = Cloudflare
access_key_id = <your-key>
secret_access_key = <your-secret>
endpoint = https://<account-id>.r2.cloudflarestorage.com
acl = private
```

### 2. Create the R2 bucket

```bash
rclone mkdir r2:my-bucket
```

## Usage

```bash
# backup to bucket root
./backup-to-r2.py /path/to/data r2:my-bucket

# backup to a subfolder in the bucket
./backup-to-r2.py /path/to/data r2:my-bucket/logseq
```

### What it does

1. **Checks prerequisites** — directory exists, `sqlite3` and `rclone` are available
2. **Safe SQLite backup** — for every `.sqlite` / `.sqlite3` / `.db` file found recursively, creates a consistent snapshot using `sqlite3 .backup` (handles WAL/journal modes safely). Backups are placed next to the original file as `<original-name>-YYYYMMDD.sqlite` (e.g. `index.sqlite` → `index-20260728.sqlite`). Old backups (>7 days) are cleaned up automatically.
3. **rclone sync** — mirrors the directory to R2, overwriting stale files and deleting remote files that no longer exist locally.

### Safety features

- **Concurrent run protection** — file lock prevents overlapping cron jobs
- **Atomic writes** — backup goes to a temp file first, then is renamed (crash-safe)
- **Idempotent** — if today's backup already exists, it's skipped
- **Partial failure** — a failing SQLite backup doesn't abort the whole run; errors are reported at the end

## Logs

Two log files are written next to the script, overwritten each run:

| File | Contents |
|---|---|
| `backup-to-r2.log` | Script-level progress and errors |
| `rclone-sync.log` | Raw rclone transfer log |

## Cron

```bash
# daily at 3 AM
0 3 * * * /full/path/to/backup-to-r2.py /full/path/to/data r2:my-bucket/subfolder
```

## Configuration

Edit the config section at the top of the script:

| Constant | Default | Notes |
|---|---|---|
| `RETENTION_DAYS` | `7` | How many daily SQLite backups to keep per database |
| `RCLONE_MODE` | `"sync"` | Change to `"copy"` for additive-only sync (won't delete remote files) |
