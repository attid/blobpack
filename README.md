# blobpack

Collect files into **one SQLite database of blobs**, so an existing database
backup tool (Databasement, or anything that can dump a sqlite file) covers them
with the encryption, retention and off-site copies it already applies to
databases.

No network access, no credentials, no cron: blobpack reads read-only mounts,
writes one sqlite file, and reports whether that file is fresh.

```text
 mounts (ro)                    blobpack                     your backup tool
 ───────────                    ────────                     ────────────────
 /src/authelia      ─┐
 /src/traefik-dyn    ├─►  tar blobs ─► collection.sqlite ─► Databasement source
 /src/ton            │   (+ sha256,     (rollback journal)   → local + S3 tiers
 /src/apprise        ─┘    timestamps)                        → retention, alerting
```

## Quick start (Docker Swarm / Compose)

```yaml
services:
  collector:
    image: ghcr.io/attid/blobpack:v1
    environment:
      - TZ=Europe/Moscow
      - BACKUP_LABEL=docker3
    volumes:
      - /apps/authelia/config:/src/authelia:ro
      - /apps/traefik/dynamic:/src/traefik-dynamic:ro
      - /apps/ton:/src/ton:ro
      - apprise_config:/src/apprise-config:ro
      - blobs:/data            # collection.sqlite lives here
    deploy:
      replicas: 1
      restart_policy:
        condition: any
    # no healthcheck needed: the image ships one
```

Then point a sqlite backup source at `/data/collection.sqlite` (read-only mount
into your backup tool). That is the whole integration.

**Which files are backed up = which paths you mount under `/src`.**
Every top-level entry of `/src` becomes one artifact named after the entry.
Nothing to parse, no quoting, and it works the same in Compose, Swarm and
Kubernetes.

## Subcommands

| Command | Purpose |
|---|---|
| `run` (default) | collect once, then follow the schedule |
| `collect` | collect once and exit (non-zero if anything failed) — use it in a Kubernetes `CronJob` |
| `list` | print stored artifacts, sizes and last successful collection |
| `restore NAME --to DIR` | extract one artifact (or `--all`) — recovery needs only this image |
| `version` | print version and build revision |

```bash
docker run --rm -v blobs:/data ghcr.io/attid/blobpack:v1 list
docker run --rm -v blobs:/data -v /tmp/out:/out \
  ghcr.io/attid/blobpack:v1 restore authelia --to /out
```

## Configuration

| Variable | Default | Meaning |
|---|---|---|
| `BACKUP_SRC_DIR` | `/src` | directory whose top-level entries become artifacts |
| `BACKUP_SOURCES` | – | explicit `name=path,name2=path2`; replaces discovery |
| `BACKUP_INCLUDE` / `BACKUP_EXCLUDE` | – | regex on artifact names |
| `BACKUP_ONE_BUNDLE` | `false` | pack the whole `BACKUP_SRC_DIR` as a single artifact |
| `BACKUP_SKIP_EMPTY` | `true` | skip empty files/directories |
| `BACKUP_DB` | `/data/collection.sqlite` | database file |
| `BACKUP_AT` | `01:00,13:00` | wall-clock collection times (local time) |
| `BACKUP_EVERY` | – | interval instead of `BACKUP_AT`, e.g. `12h` |
| `BACKUP_MAX_AGE` | `24h` | healthcheck threshold; older data = unhealthy |
| `BACKUP_HEALTH_FILE` | `.last_ok` | marker file, written only after a fully successful run |
| `BACKUP_LABEL` | hostname | stored in the database, useful when one backup tool covers several hosts |
| `BACKUP_RUNS_KEPT` | `200` | run-history rows kept |
| `BACKUP_COMPRESSION` | `gzip` | `gzip` or `none` |

## Healthcheck (shell only)

The image ships `/opt/blobpack/healthcheck.sh` and a `HEALTHCHECK` directive, so
orchestrators inherit it. It is pure `/bin/sh` — no python, no sqlite CLI:

1. the sqlite file exists and is non-empty;
2. `.last_ok` exists — the collector writes it only after a **fully successful**
   collection;
3. the marker is younger than `BACKUP_MAX_AGE`.

A dead collector therefore turns `unhealthy` within a day instead of silently
producing empty backups. Defaults: `interval=1h`, `retries=1`,
`start_period=2m`.

## The read-only trap (why rollback journal, not WAL)

The database is normally opened by another container from a **read-only** mount.
SQLite cannot open a WAL database read-only — it needs to write `-shm` next to
it, and the error is a bare `unable to open database file`. blobpack therefore
forces `journal_mode=DELETE` and never leaves `-wal`/`-shm` behind. Do not
"optimise" this to WAL.

## Database schema

```sql
artifacts(name PK, updated_at, sha256, bytes, compression, blob)
meta(key PK, value)          -- label, version, revision, last_ok_at
runs(id PK, started_at, finished_at, ok, note)   -- bounded history
```

Artifacts are upserted (one row per name) and the file is `VACUUM`ed after every
run, so the database stays at roughly the size of one snapshot instead of growing
with history.

## Failure semantics

- A configured source that is missing fails the run (no marker refresh) — a typo
  must never look like a healthy backup.
- A single unreadable artifact is recorded in `runs.note` and fails the run;
  other artifacts are still stored.
- `collect` exits non-zero when anything failed, so it is safe to use directly in
  a `CronJob`.

## Building

```bash
docker build --build-arg VERSION=$(git describe --tags --always) \
             --build-arg REVISION=$(git rev-parse --short HEAD) -t blobpack .
sh tests/run-tests.sh
```

CI (`.github/workflows/image.yml`) runs the tests and publishes
`ghcr.io/attid/blobpack` on every push to `main` and on `v*` tags
(`latest`, `sha-<short>`, semver).

## License

MIT
