#!/bin/sh
# Minimal dependency-free test suite: runs the collector against fixture trees and
# checks the database, the health probe, restore and the filter options.
set -eu

ROOT=$(cd "$(dirname "$0")/.." && pwd)
WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT

pass() { printf 'ok   %s\n' "$1"; }
fail() { printf 'FAIL %s\n' "$1" >&2; exit 1; }

collector() { python3 "$ROOT/bin/collector.py" "$@"; }
healthcheck() { sh "$ROOT/bin/healthcheck.sh"; }

# ---------------------------------------------------------------- fixtures
mkdir -p "$WORK/src/authelia/sub" "$WORK/src/empty-dir" "$WORK/src/ton"
printf 'jwt_secret: x\n' > "$WORK/src/authelia/configuration.yml"
printf 'nested\n' > "$WORK/src/authelia/sub/users.yml"
printf '{"a":1}\n' > "$WORK/src/ton/config.json"

export BACKUP_DB="$WORK/data/collection.sqlite"
export BACKUP_SRC_DIR="$WORK/src"

# ---------------------------------------------------------------- collect
collector collect >/dev/null || fail "collect exit code"
collector list > "$WORK/list.txt" || fail "list exit code"
grep -q 'authelia' "$WORK/list.txt" || fail "authelia missing from list"
grep -q 'ton' "$WORK/list.txt" || fail "ton missing from list"
grep -q 'empty-dir' "$WORK/list.txt" && fail "empty dir should be skipped by default"
pass "collect stores mounted entries and skips empty ones"

python3 - "$WORK/data/collection.sqlite" <<'PY' || fail "database content"
import sqlite3, sys
con = sqlite3.connect(sys.argv[1])
names = {row[0] for row in con.execute("select name from artifacts")}
assert names == {"authelia", "ton"}, names
journal = con.execute("pragma journal_mode").fetchone()[0]
assert journal.lower() == "delete", journal
PY
pass "database has expected artifacts and rollback journal mode"

# ------------------------------------------------------------- read-only
python3 - "$WORK/data/collection.sqlite" <<'PY' || fail "read-only open"
import sqlite3, sys
con = sqlite3.connect("file:%s?mode=ro" % sys.argv[1], uri=True)
print(con.execute("select count(*) from artifacts").fetchone()[0])
PY
pass "database opens read-only (the Databasement case)"

# --------------------------------------------------------------- restore
collector restore authelia --to "$WORK/out" >/dev/null || fail "restore exit code"
[ -f "$WORK/out/authelia/configuration.yml" ] || fail "restore layout"
grep -q 'jwt_secret' "$WORK/out/authelia/configuration.yml" || fail "restore content"
pass "restore extracts the artifact tree"

# ------------------------------------------------------------- oversize ok
collector version | grep -q blobpack || fail "version output"
pass "version subcommand"

# ------------------------------------------------------------- healthcheck
healthcheck >/dev/null || fail "healthcheck should pass right after collect"
pass "healthcheck healthy after a successful collection"

# 30 hours old: above the 24h default, below a 100d override.
touch -t "$(date -u -d '-30 hours' +%Y%m%d%H%M)" "$WORK/data/.last_ok"
if healthcheck >/dev/null 2>&1; then fail "healthcheck must fail on stale marker"; fi
pass "healthcheck unhealthy when data is older than BACKUP_MAX_AGE"

BACKUP_MAX_AGE=100d healthcheck >/dev/null || fail "BACKUP_MAX_AGE override"
pass "BACKUP_MAX_AGE override parsed"

BACKUP_MAX_AGE=12h healthcheck >/dev/null 2>&1 && fail "12h limit must reject 30h old data"
pass "BACKUP_MAX_AGE=12h rejects stale data"

rm -f "$WORK/data/collection.sqlite"
if healthcheck >/dev/null 2>&1; then fail "healthcheck must fail without database"; fi
pass "healthcheck unhealthy without database"

# ------------------------------------------------------------ explicit list
export BACKUP_SOURCES="conf=$WORK/src/authelia"
collector collect >/dev/null || fail "collect with BACKUP_SOURCES"
collector list | grep -q 'conf' || fail "BACKUP_SOURCES naming"
pass "BACKUP_SOURCES overrides discovery"
unset BACKUP_SOURCES

# --------------------------------------------------------------- exclusion
export BACKUP_EXCLUDE='^ton$'
collector collect >/dev/null || fail "collect with exclude"
collector list | grep -q 'ton' && fail "exclusion did not apply"
pass "BACKUP_EXCLUDE filters artifacts"
unset BACKUP_EXCLUDE

# ------------------------------------------------------------- one bundle
export BACKUP_ONE_BUNDLE=1
collector collect >/dev/null || fail "collect one bundle"
collector list | grep -q 'bundle' || fail "one bundle artifact missing"
pass "BACKUP_ONE_BUNDLE packs the whole tree"
unset BACKUP_ONE_BUNDLE

# ------------------------------------------------------- failure semantics
export BACKUP_SOURCES="gone=$WORK/does-not-exist"
collector collect >/dev/null 2>&1 && fail "collect with missing source must exit 1"
pass "missing source makes the run fail (marker not refreshed)"
unset BACKUP_SOURCES

printf 'all tests passed\n'
