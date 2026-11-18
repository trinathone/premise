"""Tests for premise, part 1.

Covers checkpoint creation (bundle layout, manifest contents, exclusion of
.premise and .git, .gitignore handling, running outside a git repo) and the
full round trip: checkpoint a dirty repo, mutate files and a sqlite database,
restore, and confirm the tree and database match the original, including the
automatic checkpoint taken before a restore.
"""

import contextlib
import io
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import premise


def run_git(cwd, *args):
    return subprocess.run(['git', *args], cwd=str(cwd), check=True, capture_output=True, text=True)


def make_repo():
    repo = Path(tempfile.mkdtemp())
    run_git(repo, 'init', '-q')
    run_git(repo, 'config', 'user.email', 't@t.t')
    run_git(repo, 'config', 'user.name', 't')
    (repo / 'a.txt').write_text('hello\n')
    (repo / 'sub').mkdir()
    (repo / 'sub' / 'b.txt').write_text('data\n')
    conn = sqlite3.connect(str(repo / 'app.db'))
    try:
        conn.execute('CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)')
        conn.executemany('INSERT INTO t (v) VALUES (?)', [('one',), ('two',), ('three',)])
        conn.commit()
    finally:
        conn.close()
    run_git(repo, 'add', '-A')
    run_git(repo, 'commit', '-qm', 'init')
    return repo


def make_dirty_repo():
    repo = make_repo()
    (repo / 'a.txt').write_text('dirty\n')
    (repo / 'untracked.txt').write_text('untracked\n')
    return repo


def tree_hashes(root):
    root = Path(root)
    result = {}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in ('.git', '.premise')]
        for filename in filenames:
            full = Path(dirpath) / filename
            if full.is_file():
                result[full.relative_to(root).as_posix()] = premise.sha256_file(full)
    return result


def row_count(db_path, table='t'):
    conn = sqlite3.connect(str(db_path))
    try:
        return conn.execute('SELECT COUNT(*) FROM ' + table).fetchone()[0]
    finally:
        conn.close()


def db_dump(db_path):
    # Logical dump: sqlite files are snapshotted with the online backup API,
    # so the restored file is logically identical but not byte-identical.
    conn = sqlite3.connect(str(db_path))
    try:
        return '\n'.join(conn.iterdump())
    finally:
        conn.close()


SQLITE_SUFFIXES = ('.db', '.sqlite', '.sqlite3')


def plain_hashes(root):
    # Tree hashes excluding sqlite files, which are compared by logical dump.
    return {k: v for k, v in tree_hashes(root).items()
            if not k.endswith(SQLITE_SUFFIXES)}


class TestCheckpoint(unittest.TestCase):
    def setUp(self):
        self.repo = make_repo()

    def tearDown(self):
        shutil.rmtree(self.repo, ignore_errors=True)

    def test_checkpoint_creates_bundle_and_manifest(self):
        summary = premise.checkpoint(self.repo, name='before-agent')
        self.assertEqual(summary['files'], 3)
        self.assertEqual(summary['sqlite'], 1)
        bundle = self.repo / '.premise' / 'checkpoints' / summary['id']
        self.assertTrue(bundle.is_dir())
        with open(bundle / 'manifest.json') as handle:
            manifest = json.load(handle)
        head = run_git(self.repo, 'rev-parse', 'HEAD').stdout.strip()
        self.assertEqual(manifest['git_head'], head)
        paths = [e['path'] for e in manifest['files']]
        self.assertEqual(paths, sorted(paths))
        kinds = {e['path']: e['kind'] for e in manifest['files']}
        self.assertEqual(kinds['app.db'], 'sqlite')

    def test_checkpoint_excludes_premise_and_git(self):
        first = premise.checkpoint(self.repo, name='one')
        second = premise.checkpoint(self.repo, name='two')
        bundle = self.repo / '.premise' / 'checkpoints' / second['id']
        manifest = premise.load_manifest(bundle)
        paths = [e['path'] for e in manifest['files']]
        self.assertFalse(any(p.startswith('.premise/') or p.startswith('.git/') for p in paths))
        first_manifest = premise.load_manifest(self.repo / '.premise' / 'checkpoints' / first['id'])
        self.assertFalse(any(e['path'] in paths for e in first_manifest['files'] if e['path'].startswith('.premise/')))
        self.assertNotIn(first['id'], ' '.join(paths))

    def test_checkpoint_respects_gitignore(self):
        (self.repo / '.gitignore').write_text('ignored.log\n')
        (self.repo / 'ignored.log').write_text('x\n')
        summary = premise.checkpoint(self.repo, name='ignore')
        manifest = premise.load_manifest(self.repo / '.premise' / 'checkpoints' / summary['id'])
        self.assertNotIn('ignored.log', [e['path'] for e in manifest['files']])

    def test_checkpoint_outside_repo_returns_1(self):
        plain = tempfile.mkdtemp()
        old_cwd = os.getcwd()
        try:
            os.chdir(plain)
            self.assertEqual(premise.main(['checkpoint']), 1)
        finally:
            os.chdir(old_cwd)
            shutil.rmtree(plain, ignore_errors=True)


class TestRoundTrip(unittest.TestCase):
    def setUp(self):
        self.repo = make_dirty_repo()

    def tearDown(self):
        shutil.rmtree(self.repo, ignore_errors=True)

    def test_round_trip_restores_everything(self):
        before = plain_hashes(self.repo)
        dump_before = db_dump(self.repo / 'app.db')
        rows_before = row_count(self.repo / 'app.db')
        summary = premise.checkpoint(self.repo, name='before-agent')

        (self.repo / 'a.txt').write_text('MUTATED\n')
        (self.repo / 'sub' / 'b.txt').unlink()
        (self.repo / 'newfile.txt').write_text('new\n')
        conn = sqlite3.connect(str(self.repo / 'app.db'))
        try:
            conn.execute('DROP TABLE t')
            conn.commit()
        finally:
            conn.close()
        (self.repo / 'untracked.txt').unlink()

        result = premise.restore(self.repo, summary['id'])
        self.assertEqual(result['restored'], summary['files'])
        self.assertEqual(result['deleted'], 1)
        self.assertEqual(plain_hashes(self.repo), before)
        self.assertEqual(db_dump(self.repo / 'app.db'), dump_before)
        self.assertEqual(row_count(self.repo / 'app.db'), rows_before)
        self.assertTrue((self.repo / 'sub' / 'b.txt').exists())
        self.assertTrue((self.repo / 'untracked.txt').exists())
        self.assertFalse((self.repo / 'newfile.txt').exists())

    def test_restore_creates_auto_checkpoint(self):
        summary = premise.checkpoint(self.repo, name='before-agent')
        premise.restore(self.repo, summary['id'])
        items = premise.list_checkpoints(self.repo)
        self.assertEqual(len(items), 2)
        self.assertTrue(any(i['name'] == 'auto-before-restore' for i in items))


def bundle_dir(repo, summary):
    return Path(repo) / '.premise' / 'checkpoints' / summary['id']


def corrupt_stored_file(repo, summary, rel='a.txt'):
    with open(bundle_dir(repo, summary) / 'files' / rel, 'ab') as handle:
        handle.write(b'corrupt')


def capture_main(argv):
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        code = premise.main(argv)
    return code, buffer.getvalue()


class InRepoTestCase(unittest.TestCase):
    # premise.main() locates the repo from the current directory, so these
    # tests chdir into the repo and restore the old directory afterwards.
    def make(self):
        return make_repo()

    def setUp(self):
        self.repo = self.make()
        self.old_cwd = os.getcwd()
        os.chdir(self.repo)

    def tearDown(self):
        os.chdir(self.old_cwd)
        shutil.rmtree(self.repo, ignore_errors=True)


class TestVerify(InRepoTestCase):
    def test_verify_passes_on_clean_bundle(self):
        summary = premise.checkpoint(self.repo, name='clean')
        ok, failures = premise.verify_bundle(bundle_dir(self.repo, summary))
        self.assertTrue(ok is True)
        self.assertEqual(failures, [])

    def test_verify_fails_on_corrupt_file(self):
        summary = premise.checkpoint(self.repo, name='bad')
        corrupt_stored_file(self.repo, summary)
        ok, failures = premise.verify_bundle(bundle_dir(self.repo, summary))
        self.assertTrue(ok is False)
        self.assertTrue(any('a.txt' in f for f in failures))

    def test_restore_refuses_corrupt_bundle(self):
        summary = premise.checkpoint(self.repo, name='bad')
        corrupt_stored_file(self.repo, summary)
        before = tree_hashes(self.repo)
        self.assertEqual(premise.main(['restore', summary['id']]), 2)
        self.assertEqual(tree_hashes(self.repo), before)

    def test_verify_unknown_id_returns_1(self):
        self.assertEqual(premise.main(['verify', 'no-such-id']), 1)


class TestExcludes(InRepoTestCase):
    def make(self):
        return make_dirty_repo()

    def test_symlink_escaping_repo_is_skipped(self):
        os.symlink('/etc/hostname', self.repo / 'escape.txt')
        summary = premise.checkpoint(self.repo, name='sym')
        manifest = premise.load_manifest(bundle_dir(self.repo, summary))
        self.assertNotIn('escape.txt', [e['path'] for e in manifest['files']])

    def test_restore_deletes_files_added_after_checkpoint(self):
        summary = premise.checkpoint(self.repo, name='base')
        (self.repo / 'later.txt').write_text('later\n')
        premise.restore(self.repo, summary['id'])
        self.assertFalse((self.repo / 'later.txt').exists())

    def test_no_auto_checkpoint_flag(self):
        summary = premise.checkpoint(self.repo, name='base')
        before_count = len(premise.list_checkpoints(self.repo))
        result = premise.restore(self.repo, summary['id'], no_auto_checkpoint=True)
        self.assertIsNone(result['auto_checkpoint'])
        self.assertEqual(len(premise.list_checkpoints(self.repo)), before_count)


class TestExitCodes(InRepoTestCase):
    def test_restore_unknown_id_returns_1(self):
        self.assertEqual(premise.main(['restore', 'no-such-id']), 1)

    def test_missing_argument_exits_2(self):
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as cm:
                premise.main(['restore'])
        self.assertEqual(cm.exception.code, 2)

    def test_list_empty_repo(self):
        fresh = make_repo()
        self.addCleanup(shutil.rmtree, fresh, True)
        os.chdir(fresh)
        code, text = capture_main(['list'])
        self.assertEqual(code, 0)
        self.assertIn('(no checkpoints)', text)


class TestWalAndJson(InRepoTestCase):
    def test_wal_database_round_trip(self):
        app_db = self.repo / 'app.db'
        conn = sqlite3.connect(str(app_db))
        try:
            conn.execute('PRAGMA journal_mode=WAL')
            conn.execute('INSERT INTO t (v) VALUES (?)', ('four',))
            conn.commit()
        finally:
            conn.close()
        dump_before = db_dump(app_db)
        summary = premise.checkpoint(self.repo, name='wal')
        conn = sqlite3.connect(str(app_db))
        try:
            conn.execute('INSERT INTO t (v) VALUES (?)', ('five',))
            conn.commit()
        finally:
            conn.close()
        premise.restore(self.repo, summary['id'])
        self.assertEqual(db_dump(app_db), dump_before)
        self.assertEqual(row_count(app_db), 4)
        mode = sqlite3.connect(str(app_db)).execute('PRAGMA journal_mode').fetchone()[0]
        self.assertEqual(mode, 'wal')

    def test_list_json_parses(self):
        summary = premise.checkpoint(self.repo, name='listed')
        code, text = capture_main(['list', '--json'])
        items = json.loads(text)
        self.assertEqual(code, 0)
        self.assertIsInstance(items, list)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]['id'], summary['id'])

    def test_checkpoint_json_parses(self):
        code, text = capture_main(['checkpoint', '--json', 'json-test'])
        data = json.loads(text)
        self.assertEqual(code, 0)
        self.assertTrue(data['id'].endswith('-json-test'))
        self.assertEqual(data['files'], 3)


if __name__ == '__main__':
    unittest.main()
