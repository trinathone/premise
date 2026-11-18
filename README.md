# premise

One command to checkpoint a git working tree and its sqlite databases before an agent run, and one to restore them after.

Version 1.0.0. Single file, Python standard library only.

## The pain

- In April 2026 an agent deleted a production database and its backups in about 9 seconds (the PocketOS incident). The newest off-volume backup was 3 months old, and bookings were rebuilt by hand over about 30 hours.
- Claude Code's own checkpointing docs say rewind tracks only changes made through its file-editing tools. Changes made by bash commands are not tracked, subagent edits are not restored, and external state such as databases is never captured.
- The gap: git covers tracked files, but not untracked files, and nothing covers the sqlite file sitting right next to them. Nobody ships a composed, manifest-verified, restore-rehearsed bundle for an agent session. CalmAPI checkpoints are MongoDB-only and tied to its dev server. The branch database switcher script is Postgres-only and tied to branch switching. GFS is a per-database system, not a one-shot bundle.

## What it does

- `premise checkpoint [name]` snapshots the working tree (tracked plus untracked files, excluding ignored files, `.git`, and `.premise`) and every sqlite file (`*.db`, `*.sqlite`, `*.sqlite3`) into `.premise/checkpoints/<id>/`, with a SHA-256 manifest. Sqlite files are copied with the sqlite online backup API, so the copy is consistent even for WAL-mode databases.
- `premise restore <id>` verifies the manifest first and refuses on any mismatch. It then takes an automatic checkpoint of the current state (unless you pass `--no-auto-checkpoint`), writes every bundled file back, deletes files that appeared after the checkpoint, and removes stale `-wal` and `-shm` sidecars before restoring a database.
- `premise verify <id>` re-hashes the bundle against the manifest.
- `premise list [--json]` shows checkpoints with file counts and sizes.

Exit codes: `0` ok, `1` operational failure, `2` verification or usage failure.

## Install

Python 3.10 or newer. No dependencies, no network, no LLM.

Clone the repo and run `python3 premise.py --help`, or copy `premise.py` anywhere on your `PATH`.

## Usage

```
python3 premise.py checkpoint                    # snapshot with an auto-generated id
python3 premise.py checkpoint before-agent       # snapshot with a name in the id
python3 premise.py restore 20261118-013049-before-agent   # verify, auto-checkpoint, restore
python3 premise.py verify 20261118-013049-before-agent    # re-hash the bundle
python3 premise.py list --json                   # machine-readable listing
```

## Demo

The id in the restore line comes from the output of the checkpoint line above it.

```
cd /tmp/premise-demo
git init -q . && git config user.email demo@demo.demo && git config user.name demo
printf 'hello\n' > a.txt
python3 -c "import sqlite3; c = sqlite3.connect('app.db'); c.execute('CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)'); c.executemany('INSERT INTO t (v) VALUES (?)', [('one',), ('two',), ('three',)]); c.commit(); c.close()"
git add -A && git commit -qm init
python3 premise.py checkpoint before-agent
printf 'MUTATED\n' > a.txt
python3 -c "import sqlite3; c = sqlite3.connect('app.db'); c.execute('DROP TABLE t'); c.commit(); c.close()"
python3 premise.py restore 20261118-013049-before-agent
cat a.txt
python3 -c "import sqlite3; print('rows:', sqlite3.connect('app.db').execute('SELECT COUNT(*) FROM t').fetchone()[0])"
```

```
Checkpoint 20261118-013049-before-agent: 2 files, 8198 bytes (1 sqlite)
Restored 20261118-013049-before-agent: 2 files restored, 0 deleted
Auto-checkpoint: 20261118-013050-auto-before-restore
hello
rows: 3
```

## Guarantees

- It never prints file contents, only paths, counts, sizes, and hashes.
- Restore never runs without verifying the manifest first, and always takes an auto-checkpoint first unless you pass `--no-auto-checkpoint`.
- Sqlite snapshots are logically identical (same rows, same schema). Plain files are restored byte-identical.
- No network, no LLM calls, no third-party packages.

## What it does NOT do

- Version 1 covers git working trees and sqlite files only. Compose Postgres and MySQL dumps are planned for v2. The build machine has no docker, so that path is not live-tested here and is not claimed.
- It does not checkpoint host-installed databases, Redis, or cloud state.
- It does not stop an agent from doing damage. It gives you a verified way back afterward.

## Tests

19 tests:

```
python3 -m unittest discover -s tests
```
