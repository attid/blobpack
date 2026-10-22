#!/usr/bin/env python3
"""blobpack — collect files into one SQLite database of blobs.

An existing database backup tool (for example Databasement, pgBackRest, or any
`s3cmd`/`restic` wrapper around "dump this sqlite file") already solves
encryption, retention and off-site copies. blobpack makes ordinary files ride
along: it packs each mounted path into a tar blob and stores all of them in a
single SQLite database. Back that one file up and the files are covered.

Subcommands:
    run         collect now, then follow the schedule (default)
    collect     collect once and exit
    list        list stored artifacts
    restore     extract one artifact (or everything) to a directory
    version     print version and build revision

Everything is configured through environment variables; see README.md.
"""

from __future__ import annotations

import datetime
import fnmatch
import hashlib
import io
import os
import re
import socket
import sqlite3
import sys
import tarfile
import time

VERSION = os.environ.get("BACKUP_VERSION", "dev")
REVISION = os.environ.get("BACKUP_REVISION", "unknown")

DEFAULT_SRC_DIR = "/src"
DEFAULT_DB = "/data/collection.sqlite"
DEFAULT_AT = "01:00,13:00"
DEFAULT_RUNS_KEPT = 200

SCHEMA = """
CREATE TABLE IF NOT EXISTS artifacts (
    name        TEXT PRIMARY KEY,
    updated_at  TEXT NOT NULL,
    sha256      TEXT NOT NULL,
    bytes       INTEGER NOT NULL,
    compression TEXT NOT NULL,
    blob        BLOB NOT NULL
);
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    ok          INTEGER,
    note        TEXT
);
"""


# --------------------------------------------------------------------------
# configuration helpers
# --------------------------------------------------------------------------


def env(name: str, default: str) -> str:
    value = os.environ.get(name, "")
    return value if value.strip() else default


def env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        raise SystemExit(f"{name} must be an integer, got {raw!r}")


def env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


def parse_duration(raw: str) -> int:
    """Parse `90`, `30m`, `12h`, `1d` into seconds."""
    match = re.fullmatch(r"\s*(\d+)\s*([smhd]?)\s*", raw.lower())
    if not match:
        raise SystemExit(f"cannot parse duration {raw!r} (use e.g. 24h, 90m, 3600)")
    amount = int(match.group(1))
    unit = match.group(2) or "s"
    return amount * {"s": 1, "m": 60, "h": 3600, "d": 86400}[unit]


def parse_at(raw: str) -> list[tuple[int, int]]:
    """Parse `01:00,13:00` into [(1, 0), (13, 0)]."""
    slots: list[tuple[int, int]] = []
    for chunk in raw.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        match = re.fullmatch(r"(\d{1,2}):(\d{2})", chunk)
        if not match:
            raise SystemExit(f"cannot parse BACKUP_AT entry {chunk!r} (use HH:MM)")
        hour, minute = int(match.group(1)), int(match.group(2))
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            raise SystemExit(f"BACKUP_AT entry out of range: {chunk!r}")
        slots.append((hour, minute))
    if not slots:
        raise SystemExit("BACKUP_AT is empty; use e.g. BACKUP_AT=01:00,13:00")
    return sorted(slots)


def now_iso() -> str:
    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")


def log(message: str) -> None:
    print(f"{now_iso()} {message}", flush=True)


# --------------------------------------------------------------------------
# source discovery
# --------------------------------------------------------------------------


def discover_sources() -> list[tuple[str, str]]:
    """Return [(artifact name, path)] to collect.

    Default: every top-level entry of BACKUP_SRC_DIR becomes one artifact named
    after the entry. `BACKUP_SOURCES` overrides with `name=path,name2=path2`.
    """
    explicit = os.environ.get("BACKUP_SOURCES", "").strip()
    sources: list[tuple[str, str]] = []
    if explicit:
        for chunk in explicit.split(","):
            chunk = chunk.strip()
            if not chunk:
                continue
            if "=" not in chunk:
                raise SystemExit(f"BACKUP_SOURCES entry {chunk!r} must be name=path")
            name, path = chunk.split("=", 1)
            sources.append((name.strip(), path.strip()))
    else:
        src = env("BACKUP_SRC_DIR", DEFAULT_SRC_DIR)
        if not os.path.isdir(src):
            raise SystemExit(f"source directory not found: {src}")
        if env_bool("BACKUP_ONE_BUNDLE", False):
            return [("bundle", src)]
        for entry in sorted(os.listdir(src)):
            sources.append((entry, os.path.join(src, entry)))

    include = os.environ.get("BACKUP_INCLUDE", "").strip()
    exclude = os.environ.get("BACKUP_EXCLUDE", "").strip()
    if include:
        sources = [(n, p) for n, p in sources if re.search(include, n)]
    if exclude:
        sources = [(n, p) for n, p in sources if not re.search(exclude, n)]
    return sources


def is_empty(path: str) -> bool:
    if os.path.isfile(path):
        return os.path.getsize(path) == 0
    for _root, dirs, files in os.walk(path):
        if files:
            return False
        if dirs:
            return False
    return True


# --------------------------------------------------------------------------
# packing
# --------------------------------------------------------------------------


def compression_mode() -> str:
    mode = env("BACKUP_COMPRESSION", "gzip").lower()
    if mode not in {"gzip", "none"}:
        raise SystemExit("BACKUP_COMPRESSION must be gzip or none")
    return mode


def pack(path: str, mode: str) -> bytes:
    """Pack `path` into a tar (optionally gzipped) and return the bytes."""
    buf = io.BytesIO()
    tar_mode = "w:gz" if mode == "gzip" else "w"
    with tarfile.open(fileobj=buf, mode=tar_mode) as tf:
        if os.path.isdir(path):
            base = os.path.dirname(path.rstrip("/")) or "/"
            for root, dirs, files in os.walk(path):
                dirs.sort()
                for name in sorted(files):
                    full = os.path.join(root, name)
                    tf.add(full, arcname=os.path.relpath(full, base))
        else:
            tf.add(path, arcname=os.path.basename(path))
    return buf.getvalue()


# --------------------------------------------------------------------------
# database
# --------------------------------------------------------------------------


def migrate(con: sqlite3.Connection) -> None:
    """Bring databases created by older versions (0.1.x) up to the schema.

    The 0.1.x collector shipped a single table without `compression` and without
    `meta`, and it is already deployed on a live host, so upgrading the image
    must not require a fresh volume.
    """
    columns = {row[1] for row in con.execute("PRAGMA table_info(artifacts)")}
    if columns and "compression" not in columns:
        con.execute(
            "ALTER TABLE artifacts ADD COLUMN compression TEXT NOT NULL DEFAULT 'gzip'"
        )
        con.commit()


def connect(db_path: str) -> sqlite3.Connection:
    os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
    con = sqlite3.connect(db_path, timeout=60)
    # Rollback journal, never WAL: the database is usually read by another
    # container from a read-only mount, and SQLite cannot open a WAL database
    # read-only (it has to write -shm next to the file).
    con.execute("PRAGMA journal_mode=DELETE")
    con.executescript(SCHEMA)
    migrate(con)
    return con


def collect() -> int:
    db_path = env("BACKUP_DB", DEFAULT_DB)
    started = now_iso()
    mode = compression_mode()
    sources = discover_sources()
    skip_empty = env_bool("BACKUP_SKIP_EMPTY", True)
    label = env("BACKUP_LABEL", socket.gethostname())

    con = connect(db_path)
    ok = 1
    notes: list[str] = []
    try:
        run_id = con.execute(
            "INSERT INTO runs(started_at) VALUES (?)", (started,)
        ).lastrowid
        con.commit()

        stored = 0
        for name, path in sources:
            try:
                if not os.path.exists(path):
                    # A configured source that vanished is an error, not a skip:
                    # otherwise a typo produces a "healthy" backup without data.
                    ok = 0
                    notes.append(f"{name}:missing")
                    continue
                if skip_empty and is_empty(path):
                    notes.append(f"{name}:empty-skipped")
                    continue
                data = pack(path, mode)
                con.execute(
                    "INSERT OR REPLACE INTO artifacts"
                    "(name, updated_at, sha256, bytes, compression, blob)"
                    " VALUES (?,?,?,?,?,?)",
                    (
                        name,
                        now_iso(),
                        hashlib.sha256(data).hexdigest(),
                        len(data),
                        mode,
                        data,
                    ),
                )
                stored += 1
                notes.append(f"{name}:{len(data)}B")
            except Exception as exc:  # keep collecting the remaining sources
                ok = 0
                notes.append(f"{name}:error={exc}")

        if not sources:
            ok = 0
            notes.append("no sources found")

        for key, value in (
            ("label", label),
            ("version", VERSION),
            ("revision", REVISION),
            ("last_ok_at", now_iso() if ok else ""),
            ("last_run_at", now_iso()),
        ):
            if value or key == "last_ok_at":
                con.execute(
                    "INSERT OR REPLACE INTO meta(key, value) VALUES (?,?)",
                    (key, value),
                )

        con.execute(
            "DELETE FROM runs WHERE id NOT IN "
            "(SELECT id FROM runs ORDER BY id DESC LIMIT ?)",
            (env_int("BACKUP_RUNS_KEPT", DEFAULT_RUNS_KEPT),),
        )
        con.execute(
            "UPDATE runs SET finished_at=?, ok=?, note=? WHERE id=?",
            (now_iso(), ok, "; ".join(notes), run_id),
        )
        con.commit()
        con.execute("VACUUM")
    finally:
        con.close()

    if ok:
        marker = os.path.join(
            os.path.dirname(db_path), env("BACKUP_HEALTH_FILE", ".last_ok")
        )
        try:
            with open(marker, "w", encoding="utf-8") as fh:
                fh.write(str(int(time.time())))
        except OSError as exc:
            log(f"warning: cannot write health marker {marker}: {exc}")

    log(
        f"collect ok={ok} label={label} artifacts={stored}/{len(sources)}"
        f" db={os.path.getsize(db_path)}B note={'; '.join(notes)}"
    )
    return ok


# --------------------------------------------------------------------------
# subcommands
# --------------------------------------------------------------------------


def cmd_list() -> int:
    db_path = env("BACKUP_DB", DEFAULT_DB)
    if not os.path.exists(db_path):
        print(f"no database at {db_path}", file=sys.stderr)
        return 1
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        rows = con.execute(
            "SELECT name, bytes, compression, updated_at FROM artifacts ORDER BY name"
        ).fetchall()
        meta = dict(con.execute("SELECT key, value FROM meta").fetchall())
    finally:
        con.close()
    print(f"database: {db_path}")
    print(f"label: {meta.get('label', '?')} version: {meta.get('version', '?')}")
    print(f"last successful collection: {meta.get('last_ok_at', 'never')}")
    for name, size, compression, updated in rows:
        print(f"{name:<28} {size:>10}B {compression:<6} {updated}")
    return 0


def safe_members(tf: tarfile.TarFile) -> list[tarfile.TarInfo]:
    members = []
    for member in tf.getmembers():
        target = os.path.normpath(member.name)
        if target.startswith("..") or os.path.isabs(target):
            raise SystemExit(f"refusing unsafe path in archive: {member.name}")
        members.append(member)
    return members


def cmd_restore(args: list[str]) -> int:
    if not args:
        print("usage: restore NAME --to DIR | restore --all --to DIR", file=sys.stderr)
        return 2
    dest: str | None = None
    names: list[str] = []
    index = 0
    while index < len(args):
        arg = args[index]
        if arg == "--to":
            index += 1
            if index >= len(args):
                print("--to needs a directory", file=sys.stderr)
                return 2
            dest = args[index]
        elif arg == "--all":
            names.append("")
        else:
            names.append(arg)
        index += 1
    if not dest:
        print("--to DIR is required", file=sys.stderr)
        return 2

    db_path = env("BACKUP_DB", DEFAULT_DB)
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        if names == [""]:
            rows = con.execute("SELECT name, blob FROM artifacts ORDER BY name").fetchall()
        else:
            rows = []
            for name in names:
                row = con.execute(
                    "SELECT name, blob FROM artifacts WHERE name=?", (name,)
                ).fetchone()
                if row is None:
                    print(f"no such artifact: {name}", file=sys.stderr)
                    return 1
                rows.append(row)
    finally:
        con.close()

    os.makedirs(dest, exist_ok=True)
    for name, blob in rows:
        with tarfile.open(fileobj=io.BytesIO(blob), mode="r:*") as tf:
            tf.extractall(path=dest, members=safe_members(tf))
        print(f"restored {name} -> {dest}")
    return 0


def cmd_version() -> int:
    print(f"blobpack {VERSION} ({REVISION})")
    return 0


def next_run(slots: list[tuple[int, int]], now: datetime.datetime) -> datetime.datetime:
    candidates: list[datetime.datetime] = []
    for day_offset in (0, 1):
        day = now + datetime.timedelta(days=day_offset)
        for hour, minute in slots:
            candidates.append(
                day.replace(hour=hour, minute=minute, second=0, microsecond=0)
            )
    return min(c for c in candidates if c > now)


def cmd_run() -> int:
    ok = collect()
    interval_raw = os.environ.get("BACKUP_EVERY", "").strip()
    interval = parse_duration(interval_raw) if interval_raw else None
    slots = None if interval else parse_at(env("BACKUP_AT", DEFAULT_AT))

    while True:
        now = datetime.datetime.now().astimezone()
        if slots:
            target = next_run(slots, now)
        else:
            target = now + datetime.timedelta(seconds=interval or 0)
        delay = (target - now).total_seconds()
        log(f"next run at {target.isoformat()} (sleep {int(delay)}s)")
        time.sleep(max(30.0, delay))
        ok = collect()


def main(argv: list[str]) -> int:
    command = argv[1] if len(argv) > 1 else "run"
    rest = argv[2:]
    if command == "version":
        return cmd_version()
    if command == "list":
        return cmd_list()
    if command == "restore":
        return cmd_restore(rest)
    if command == "collect":
        return 0 if collect() else 1
    if command == "run":
        return cmd_run()
    print(f"unknown command: {command}", file=sys.stderr)
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
