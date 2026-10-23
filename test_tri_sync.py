#!/usr/bin/env python3
"""tri-sync tests: two temporary git repos (LOCAL = subfolder of a bigger repo, REMOTE =
independent repo) run the full sync(). Run: python3 test_tri_sync.py"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unicodedata
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import tri_sync as ts  # noqa: E402

SCOPE = ["wiki", "raw"]
LOG_BASE = ("## [2026-10-20] ingest | A\n- a\n\n"
            "## [2026-10-18] ingest | B\n- b\n")
HANGUL = "wiki/한국어-페이지.md"


def sh(cwd, *args):
    subprocess.run(args, cwd=cwd, check=True, capture_output=True)


def write(root, rel, text):
    p = os.path.join(root, rel)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w") as fh:
        fh.write(text)


def read(root, rel):
    with open(os.path.join(root, rel)) as fh:
        return fh.read()


def commit(repo, msg="edit"):
    sh(repo, "git", "add", "-A")
    sh(repo, "git", "commit", "-q", "-m", msg)


class SyncTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.vault = os.path.join(self.tmp, "vault")
        self.a = os.path.join(self.vault, "03 - Resources", "notes")
        self.r = os.path.join(self.tmp, "remote")
        self.sd = os.path.join(self.tmp, "state")
        files = {"wiki/log.md": LOG_BASE, "wiki/頁面.md": "original\n", "raw/a.txt": "raw\n"}
        files.update({"raw/bulk/%02d.txt" % i: "x\n" for i in range(12)})
        for repo, root, nfd in ((self.vault, self.a, False), (self.r, self.r, True)):
            os.makedirs(root)
            sh(repo, "git", "init", "-q")
            sh(repo, "git", "config", "user.email", "t@example.com")
            sh(repo, "git", "config", "user.name", "t")
            sh(repo, "git", "config", "core.hooksPath", "/dev/null")
            for rel, txt in files.items():
                write(root, rel, txt)
            # the remote stores the Hangul name as NFD, like macOS-created files often are
            write(root, unicodedata.normalize("NFD", HANGUL) if nfd else HANGUL, "한글\n")
            write(root, "scratch/untouched.md", "outside scope\n")
            commit(repo, "init")
        self.cfg = dict(ts.load_cfg(), local_root=self.a, state=self.sd, scope=SCOPE,
                        union=["log.md"], max_del_files=10, max_del_lines=1500)
        ts.cmd_init_base(self.cfg, "HEAD", "init")
        self.A = ts.LocalSide(self.a, SCOPE, "local")
        self.R = ts.LocalSide(self.r, SCOPE, "remote")

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def run_sync(self, remote=None):
        return ts.sync(self.A, remote or self.R, self.sd, self.cfg, log=lambda *_: None)

    def assert_equal_sides(self):
        self.assertEqual(self.A.manifest(), self.R.manifest())

    def test_nfd_unchanged_is_noop(self):
        self.assertEqual(self.run_sync()["plan"], {})
        self.assertTrue(any(f != unicodedata.normalize("NFC", f)
                            for f in os.listdir(os.path.join(self.r, "wiki"))))

    def test_one_side_edit_and_add(self):
        write(self.a, "wiki/頁面.md", "local edit\n")
        commit(self.vault)
        write(self.r, "wiki/new.md", "remote add\n")
        commit(self.r)
        res = self.run_sync()
        self.assertEqual(res["plan"], {"to_remote": 1, "to_local": 1})
        self.assertEqual(read(self.r, "wiki/頁面.md"), "local edit\n")
        self.assertEqual(read(self.a, "wiki/new.md"), "remote add\n")
        self.assert_equal_sides()
        self.assertEqual(read(self.r, "scratch/untouched.md"), "outside scope\n")
        st = ts.load_state(self.sd)
        self.assertEqual(st["remote_commit"], ts.git(self.r, "rev-parse", "HEAD").strip())
        self.assertEqual(st["local_commit"], ts.git(self.vault, "rev-parse", "HEAD").strip())
        self.assertEqual(self.run_sync()["plan"], {})

    def test_both_edit_log_union_sorted(self):
        write(self.a, "wiki/log.md", "## [2026-10-23] ingest | L\n- l\n\n" + LOG_BASE)
        commit(self.vault)
        write(self.r, "wiki/log.md", "## [2026-10-21] ingest | R\n- r\n\n" + LOG_BASE)
        commit(self.r)
        self.assertEqual(self.run_sync()["plan"], {"union": 1})
        heads = [ln for ln in read(self.a, "wiki/log.md").splitlines() if ln.startswith("## [")]
        self.assertEqual(heads, ["## [2026-10-23] ingest | L", "## [2026-10-21] ingest | R",
                                 "## [2026-10-20] ingest | A", "## [2026-10-18] ingest | B"])
        self.assert_equal_sides()
        # second round uses the saved baseline log: still a union, no duplicates
        write(self.r, "wiki/log.md", "## [2026-10-24] x | R2\n- y\n\n" + read(self.r, "wiki/log.md"))
        commit(self.r)
        write(self.a, "wiki/log.md", read(self.a, "wiki/log.md") + "\n## [2026-08-01] old | L2\n- z\n")
        commit(self.vault)
        self.run_sync()
        heads = [ln for ln in read(self.r, "wiki/log.md").splitlines() if ln.startswith("## [")]
        self.assertEqual(len(heads), 6)
        self.assertEqual(len(set(heads)), 6)
        self.assertEqual(heads[0], "## [2026-10-24] x | R2")
        self.assert_equal_sides()

    def test_nfd_filename_edit_in_place(self):
        write(self.a, HANGUL, "edited\n")
        commit(self.vault)
        self.assertEqual(self.run_sync()["plan"], {"to_remote": 1})
        hits = [f for f in os.listdir(os.path.join(self.r, "wiki"))
                if unicodedata.normalize("NFC", f) == os.path.basename(HANGUL)]
        self.assertEqual(len(hits), 1)  # no second copy, nothing deleted
        self.assertEqual(read(self.r, os.path.join("wiki", hits[0])), "edited\n")
        self.assert_equal_sides()

    def test_nfd_side_delete(self):
        os.remove(os.path.join(self.r, unicodedata.normalize("NFD", HANGUL)))
        commit(self.r)
        self.assertEqual(self.run_sync()["plan"], {"del_local": 1})
        self.assertFalse(os.path.exists(os.path.join(self.a, HANGUL)))
        self.assert_equal_sides()
        self.assertNotIn(HANGUL, ts.load_state(self.sd)["base"])

    def test_delete_from_local(self):
        os.remove(os.path.join(self.a, "raw/a.txt"))
        commit(self.vault)
        self.run_sync()
        self.assertFalse(os.path.exists(os.path.join(self.r, "raw/a.txt")))
        self.assert_equal_sides()

    def test_conflict_not_overwritten(self):
        write(self.a, "wiki/頁面.md", "L\n")
        commit(self.vault)
        write(self.r, "wiki/頁面.md", "R\n")
        commit(self.r)
        self.assertEqual(self.run_sync()["conflicts"], ["wiki/頁面.md"])
        self.assertEqual(read(self.a, "wiki/頁面.md"), "L\n")
        self.assertEqual(read(self.r, "wiki/頁面.md"), "R\n")
        self.assertEqual(ts.classify(ts.load_state(self.sd)["base"], self.A.manifest(),
                                     self.R.manifest())["conflict"], ["wiki/頁面.md"])

    def test_mass_delete_plan_guard(self):
        shutil.rmtree(os.path.join(self.r, "raw/bulk"))
        commit(self.r)
        with self.assertRaises(ts.Abort) as cm:
            self.run_sync()
        self.assertIn("GUARD too many planned deletions", str(cm.exception))
        self.assertEqual(len(os.listdir(os.path.join(self.a, "raw/bulk"))), 12)

    def test_numstat_guard_restores(self):
        big = "".join("line %d\n" % i for i in range(2000))
        write(self.a, "wiki/big.md", big)
        write(self.r, "wiki/big.md", big)
        commit(self.vault)
        commit(self.r)
        self.run_sync()
        write(self.r, "wiki/big.md", "emptied\n")
        write(self.r, "wiki/added.md", "new\n")
        commit(self.r)
        head = ts.git(self.vault, "rev-parse", "HEAD")
        with self.assertRaises(ts.Abort) as cm:
            self.run_sync()
        self.assertIn("numstat", str(cm.exception))
        self.assertEqual(ts.git(self.vault, "rev-parse", "HEAD"), head)
        self.assertEqual(ts.git(self.vault, "status", "--porcelain"), "")
        self.assertEqual(len(read(self.a, "wiki/big.md").splitlines()), 2000)

    def test_gitignored_not_synced(self):
        for root, repo in ((self.a, self.vault), (self.r, self.r)):
            write(root, ".gitignore", "wiki/assets/*.png\n")
            commit(repo)
        write(self.r, "wiki/assets/local.png", "png")
        self.assertEqual(self.run_sync()["plan"], {})
        self.assertFalse(os.path.exists(os.path.join(self.a, "wiki/assets/local.png")))

    def test_dirty_side_skips(self):
        write(self.r, "wiki/頁面.md", "uncommitted\n")
        with self.assertRaises(ts.Abort) as cm:
            self.run_sync()
        self.assertTrue(str(cm.exception).startswith("SKIP remote"))

    def test_remote_transport(self):
        """Full sync through RemoteSide, with `sh -c` standing in for ssh."""
        script = os.path.join(self.tmp, "remote-bin", "tri_sync.py")
        rem = ts.RemoteSide("sh -c", self.r, SCOPE, sys.executable, script)
        rem.install()
        write(self.a, "wiki/頁面.md", "via transport\n")
        commit(self.vault)
        write(self.r, "raw/b.txt", "from remote\n")
        commit(self.r)
        res = self.run_sync(rem)
        self.assertEqual(res["plan"], {"to_remote": 1, "to_local": 1})
        self.assertEqual(read(self.r, "wiki/頁面.md"), "via transport\n")
        self.assertEqual(read(self.a, "raw/b.txt"), "from remote\n")
        self.assert_equal_sides()

    def test_remote_unreachable(self):
        rem = ts.RemoteSide("false", self.r, SCOPE)
        with self.assertRaises(ts.RemoteUnreachable):
            rem.manifest()

    def test_side_main_roundtrip(self):
        r = subprocess.run([sys.executable, "-I", ts.__file__, "side", self.r, ",".join(SCOPE), "manifest"],
                           input=b"{}", capture_output=True, check=True)
        resp = json.loads(r.stdout.decode().rsplit(ts.SENTINEL, 1)[1])
        self.assertEqual(resp["res"], self.R.manifest())


class MergeLogTest(unittest.TestCase):
    def test_edit_one_entry_replaces(self):
        out = ts.merge_log(LOG_BASE, LOG_BASE.replace("- a\n", "- a edited\n"), LOG_BASE)
        self.assertIn("- a edited", out)
        self.assertNotIn("- a\n", out)

    def test_existing_entries_byte_identical(self):
        local = "## [2026-10-20] a\n- a\n## [2026-10-12] b\n- b\n## [2026-10-13] c\n- c\n"
        remote = "## [2026-10-21] new\n- n\n\n" + local
        self.assertEqual(ts.merge_log(local, local, remote), "## [2026-10-21] new\n- n\n\n" + local)

    def test_no_base_is_union(self):
        out = ts.merge_log(None, "## [2026-01-02] x\n", "## [2026-01-03] y\n")
        self.assertEqual([ln for ln in out.splitlines() if ln], ["## [2026-01-03] y", "## [2026-01-02] x"])

    def test_union_patterns(self):
        self.assertEqual(ts.classify({"a/CHANGELOG.md": "b"}, {"a/CHANGELOG.md": "l"},
                                     {"a/CHANGELOG.md": "r"}, ["CHANGE*.md"])["union"], ["a/CHANGELOG.md"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
