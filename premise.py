"""premise: one-command checkpoint of a repo before an agent run.

premise takes a snapshot of a git repository working tree (tracked files plus
untracked files that are not ignored) and of any sqlite databases inside it.
Run it before letting an agent loose on your project, and the saved state can
be restored afterwards if the run goes wrong.

Each checkpoint is a bundle under .premise/checkpoints/<id>/ holding a copy of
every file and a manifest.json describing what was stored, with sha256 hashes
so the copy can be verified later. SQLite files are copied with the online
backup API so the copy is consistent even if a database is in use.

Deterministic, stdlib only, no network.
"""

import argparse
import datetime
import hashlib
import json
import pathlib
import re
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

VERSION = '1.0.0'
PREMISE_DIR = '.premise'
CHECKPOINTS_DIR = '.premise/checkpoints'
SQLITE_SUFFIXES = ('.db', '.sqlite', '.sqlite3')


class PremiseError(Exception):
    pass


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, 'rb') as handle:
        while True:
            chunk = handle.read(64 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _run_git(root: Path, args: list) -> str | None:
    try:
        result = subprocess.run(
            ['git'] + args,
            cwd=root,
            capture_output=True,
            text=True,
            check=False,
        )
    except Exception:
        return None
    if result.returncode != 0:
        return None
    return result.stdout


def find_repo_root(start: Path) -> Path | None:
    try:
        out = _run_git(start, ['rev-parse', '--show-toplevel'])
    except Exception:
        return None
    if out is None or not out.strip():
        return None
    return Path(out.strip())


def git_head(root: Path) -> str:
    out = _run_git(root, ['rev-parse', 'HEAD'])
    return out.strip() if out else ''


def git_dirty(root: Path) -> list:
    out = _run_git(root, ['status', '--porcelain'])
    if not out:
        return []
    return [line.rstrip() for line in out.splitlines() if line.strip()]


def _git_paths(root: Path, args: list) -> list:
    out = _run_git(root, args)
    if not out:
        return []
    return [line for line in out.split('\n') if line]


def _is_excluded(rel: str) -> bool:
    return rel.startswith('.premise/') or rel.startswith('.git/')


def _stays_inside(root: Path, rel: str) -> bool:
    full = root / rel
    if not full.is_symlink():
        return True
    try:
        full.resolve().relative_to(root.resolve())
    except (ValueError, OSError, RuntimeError):
        return False
    return True


def enumerate_files(root: Path) -> list:
    found = set(_git_paths(root, ['ls-files']))
    found.update(_git_paths(root, ['ls-files', '--others', '--exclude-standard']))
    result = set()
    for rel in found:
        if _is_excluded(rel):
            continue
        if not _stays_inside(root, rel):
            continue
        full = root / rel
        if not full.is_file():
            continue
        result.add(pathlib.PurePath(rel).as_posix())
    return sorted(result)


def is_sqlite_file(rel_path: str) -> bool:
    return pathlib.PurePath(rel_path).suffix.lower() in SQLITE_SUFFIXES


def _integrity_ok(path: Path) -> bool:
    conn = sqlite3.connect(str(path))
    try:
        rows = conn.execute('PRAGMA integrity_check').fetchall()
    finally:
        conn.close()
    return len(rows) == 1 and rows[0][0] == 'ok'


def snapshot_sqlite(src: Path, dst: Path) -> bool:
    source = None
    target = None
    try:
        source = sqlite3.connect('file:' + str(src) + '?mode=ro', uri=True)
        target = sqlite3.connect(str(dst))
        source.backup(target)
        target.close()
        target = None
        source.close()
        source = None
        return _integrity_ok(dst)
    except Exception:
        return False
    finally:
        for conn in (target, source):
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass


def sanitize_name(name: str | None) -> str | None:
    if name is None:
        return None
    cleaned = re.sub(r'[^A-Za-z0-9._-]', '', name)
    if not cleaned:
        raise PremiseError('invalid checkpoint name')
    return cleaned


def _ensure_excluded(root: Path) -> None:
    try:
        exclude = root / '.git' / 'info' / 'exclude'
        exclude.parent.mkdir(parents=True, exist_ok=True)
        existing = exclude.read_text() if exclude.exists() else ''
        if '.premise/' in existing.splitlines():
            return
        prefix = '' if not existing or existing.endswith('\n') else '\n'
        with open(exclude, 'a') as handle:
            handle.write(prefix + '.premise/\n')
    except Exception:
        pass


def _store_file(src: Path, dst: Path, rel: str) -> str:
    dst.parent.mkdir(parents=True, exist_ok=True)
    if is_sqlite_file(rel) and snapshot_sqlite(src, dst):
        return 'sqlite'
    shutil.copyfile(src, dst)
    return 'file'


def checkpoint(root: Path, name: str | None = None) -> dict:
    clean = sanitize_name(name)
    now = datetime.datetime.now(datetime.timezone.utc)
    cp_id = now.strftime('%Y%m%d-%H%M%S')
    if clean:
        cp_id += '-' + clean
    base_id = cp_id
    suffix = 1
    while (root / CHECKPOINTS_DIR / cp_id).exists():
        suffix += 1
        cp_id = base_id + '-' + str(suffix)
    bundle = root / CHECKPOINTS_DIR / cp_id
    files_dir = bundle / 'files'
    files_dir.mkdir(parents=True, exist_ok=True)
    _ensure_excluded(root)

    entries = []
    for rel in enumerate_files(root):
        dst = files_dir / rel
        kind = _store_file(root / rel, dst, rel)
        entries.append({
            'path': rel,
            'kind': kind,
            'sha256': sha256_file(dst),
            'size': dst.stat().st_size,
        })
    entries.sort(key=lambda e: e['path'])

    head = git_head(root)
    manifest = {
        'version': VERSION,
        'id': cp_id,
        'name': clean,
        'created': now.strftime('%Y-%m-%dT%H:%M:%SZ'),
        'git_head': head,
        'dirty': git_dirty(root),
        'files': entries,
    }
    with open(bundle / 'manifest.json', 'w') as handle:
        json.dump(manifest, handle, indent=2, sort_keys=True)
        handle.write('\n')

    return {
        'id': cp_id,
        'name': clean,
        'files': len(entries),
        'sqlite': sum(1 for e in entries if e['kind'] == 'sqlite'),
        'bytes': sum(e['size'] for e in entries),
        'git_head': head,
        'path': str(bundle),
    }


class VerificationError(PremiseError):
    pass


def load_manifest(bundle_dir: Path) -> dict:
    manifest_path = bundle_dir / 'manifest.json'
    if not manifest_path.is_file():
        raise PremiseError('checkpoint not found: ' + str(bundle_dir))
    try:
        with open(manifest_path) as handle:
            manifest = json.load(handle)
    except (ValueError, OSError) as exc:
        raise PremiseError('invalid manifest: ' + str(exc))
    if not isinstance(manifest, dict):
        raise PremiseError('invalid manifest: not an object')
    for key in ('version', 'id', 'files'):
        if key not in manifest:
            raise PremiseError('invalid manifest: missing key ' + key)
    return manifest


def verify_bundle(bundle_dir: Path) -> tuple:
    manifest = load_manifest(bundle_dir)
    failures = []
    for entry in manifest['files']:
        stored = bundle_dir / 'files' / entry['path']
        if not stored.is_file():
            failures.append('missing: ' + entry['path'])
        elif sha256_file(stored) != entry['sha256']:
            failures.append('hash mismatch: ' + entry['path'])
    return (not failures, failures)


def list_checkpoints(root: Path) -> list:
    base = root / CHECKPOINTS_DIR
    if not base.exists():
        return []
    result = []
    for child in sorted(base.iterdir()):
        if not child.is_dir():
            continue
        try:
            manifest = load_manifest(child)
        except PremiseError:
            continue
        result.append({
            'id': manifest['id'],
            'name': manifest.get('name'),
            'created': manifest.get('created'),
            'files': len(manifest['files']),
            'bytes': sum(e.get('size', 0) for e in manifest['files']),
        })
    result.sort(key=lambda item: item['id'])
    return result


def find_bundle(root: Path, checkpoint_id: str) -> Path:
    bundle = root / CHECKPOINTS_DIR / checkpoint_id
    if not bundle.is_dir():
        raise PremiseError('no checkpoint with id: ' + checkpoint_id)
    return bundle


def _prune_empty_dirs(root: Path, start: Path) -> None:
    protected = {root, root / '.git', root / PREMISE_DIR}
    current = start
    while current not in protected and current != current.parent:
        try:
            current.rmdir()
        except OSError:
            return
        current = current.parent


def _restore_entry(bundle: Path, root: Path, entry: dict) -> None:
    src = bundle / 'files' / entry['path']
    dst = root / entry['path']
    dst.parent.mkdir(parents=True, exist_ok=True)
    if entry.get('kind') == 'sqlite':
        for extra in ('-wal', '-shm'):
            sidecar = Path(str(dst) + extra)
            if sidecar.exists():
                sidecar.unlink()
    shutil.copyfile(src, dst)
    if sha256_file(dst) != entry['sha256']:
        raise PremiseError('restore failed for: ' + entry['path'])


def restore(root: Path, checkpoint_id: str, no_auto_checkpoint: bool = False) -> dict:
    bundle = find_bundle(root, checkpoint_id)
    manifest = load_manifest(bundle)
    ok, failures = verify_bundle(bundle)
    if not ok:
        raise VerificationError('checkpoint failed verification: ' + '; '.join(failures))
    auto_id = None
    if not no_auto_checkpoint:
        auto_id = checkpoint(root, name='auto-before-restore')['id']

    for entry in manifest['files']:
        _restore_entry(bundle, root, entry)

    current = set(enumerate_files(root))
    wanted = set(entry['path'] for entry in manifest['files'])
    extras = sorted(current - wanted)
    for rel in extras:
        (root / rel).unlink()
        _prune_empty_dirs(root, (root / rel).parent)

    return {
        'id': checkpoint_id,
        'restored': len(manifest['files']),
        'deleted': len(extras),
        'auto_checkpoint': auto_id,
        'bytes': sum(e['size'] for e in manifest['files']),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog='premise',
        description='Checkpoint a repo plus its sqlite databases before an agent run, and restore it after.',
    )
    parser.add_argument('--version', action='version', version='premise ' + VERSION)
    sub = parser.add_subparsers(dest='command', required=True)

    cp = sub.add_parser('checkpoint', help='save a checkpoint')
    cp.add_argument('name', nargs='?')
    cp.add_argument('--json', action='store_true')

    rs = sub.add_parser('restore', help='restore a checkpoint')
    rs.add_argument('id')
    rs.add_argument('--no-auto-checkpoint', action='store_true')
    rs.add_argument('--json', action='store_true')

    vf = sub.add_parser('verify', help='verify a checkpoint bundle')
    vf.add_argument('id')

    ls = sub.add_parser('list', help='list checkpoints')
    ls.add_argument('--json', action='store_true')
    return parser


def _cmd_checkpoint(root: Path, args: argparse.Namespace) -> int:
    summary = checkpoint(root, args.name)
    if args.json:
        print(json.dumps(summary, indent=2))
    else:
        print('Checkpoint %s: %d files, %d bytes (%d sqlite)' % (
            summary['id'], summary['files'], summary['bytes'], summary['sqlite']))
    return 0


def _cmd_restore(root: Path, args: argparse.Namespace) -> int:
    summary = restore(root, args.id, no_auto_checkpoint=args.no_auto_checkpoint)
    if args.json:
        print(json.dumps(summary, indent=2))
        return 0
    print('Restored %s: %d files restored, %d deleted' % (
        summary['id'], summary['restored'], summary['deleted']))
    if summary['auto_checkpoint']:
        print('Auto-checkpoint: ' + summary['auto_checkpoint'])
    return 0


def _cmd_verify(root: Path, args: argparse.Namespace) -> int:
    ok, failures = verify_bundle(find_bundle(root, args.id))
    if ok:
        print('OK: ' + args.id)
        return 0
    for failure in failures:
        print(failure)
    return 2


def _cmd_list(root: Path, args: argparse.Namespace) -> int:
    items = list_checkpoints(root)
    if args.json:
        print(json.dumps(items, indent=2))
    elif not items:
        print('(no checkpoints)')
    else:
        for item in items:
            print('%s  %s  %d files  %d bytes' % (
                item['id'], item['name'] or '-', item['files'], item['bytes']))
    return 0


COMMANDS = {
    'checkpoint': _cmd_checkpoint,
    'restore': _cmd_restore,
    'verify': _cmd_verify,
    'list': _cmd_list,
}


def main(argv: list | None = None) -> int:
    args = build_parser().parse_args(argv)
    root = find_repo_root(Path.cwd())
    if root is None:
        print('error: not inside a git repository', file=sys.stderr)
        return 1
    try:
        return COMMANDS[args.command](root, args)
    except VerificationError as exc:
        print('error: ' + str(exc), file=sys.stderr)
        return 2
    except PremiseError as exc:
        print('error: ' + str(exc), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
