#!/usr/bin/env python3
"""Update Now in a git checkout keeps the user's own commits and changes."""
import os
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import gi
gi.require_version("Gtk", "3.0")
gi.require_version("Vte", "2.91")

import tabit  # noqa: E402


def git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True, text=True,
                          capture_output=True).stdout.strip()


class TestUpdatePull(unittest.TestCase):

    def setUp(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        env = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
               "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
               "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1"}
        old = {k: os.environ.get(k) for k in env}
        os.environ.update(env)
        self.addCleanup(lambda: [os.environ.pop(k) if v is None else
                                 os.environ.__setitem__(k, v) for k, v in old.items()])
        self.up = os.path.join(d.name, "up")        # GitHub
        self.me = os.path.join(d.name, "me")        # the user's checkout
        os.makedirs(self.up)
        git(self.up, "init", "-q", "-b", "main")
        self.write(self.up, "a.txt", "one\n")
        self.write(self.up, "b.txt", "b\n")
        self.commit(self.up, "first")
        git(d.name, "clone", "-q", self.up, self.me)
        self.log = []

    def write(self, repo, name, text):
        with open(os.path.join(repo, name), "w") as f:
            f.write(text)

    def read(self, name):
        with open(os.path.join(self.me, name)) as f:
            return f.read()

    def commit(self, repo, msg):
        git(repo, "add", "-A")
        git(repo, "commit", "-q", "-m", msg)
        return git(repo, "rev-parse", "HEAD")

    def pull(self):
        tabit._update_pull(self.me, self.log.append)

    def new_upstream(self):
        self.write(self.up, "c.txt", "new\n")
        return self.commit(self.up, "release")

    def test_fast_forward(self):
        new = self.new_upstream()
        self.pull()
        self.assertEqual(git(self.me, "rev-parse", "HEAD"), new)

    def test_own_commit_goes_on_top(self):
        # the colleague's case: a fix committed on main, never pushed
        self.write(self.me, "b.txt", "my fix\n")
        self.commit(self.me, "my fix")
        new = self.new_upstream()
        self.pull()
        self.assertEqual(git(self.me, "rev-parse", "HEAD~1"), new)
        self.assertEqual(git(self.me, "log", "-1", "--format=%s"), "my fix")
        self.assertEqual(self.read("b.txt"), "my fix\n")
        self.assertEqual(self.read("c.txt"), "new\n")

    def test_uncommitted_change_is_kept(self):
        self.write(self.me, "b.txt", "editing\n")
        self.write(self.up, "b.txt", "b\n")       # unchanged upstream
        self.write(self.up, "a.txt", "two\n")
        new = self.commit(self.up, "release")
        self.pull()
        self.assertEqual(git(self.me, "rev-parse", "HEAD"), new)
        self.assertEqual(self.read("b.txt"), "editing\n")

    def test_pushed_commit_is_merged_not_rewritten(self):
        self.write(self.me, "b.txt", "feature\n")
        mine = self.commit(self.me, "feature")
        git(self.me, "push", "-q", "origin", "HEAD:refs/heads/feature")
        git(self.me, "fetch", "-q", "origin")
        self.new_upstream()
        self.pull()
        self.assertEqual(subprocess.run(
            ["git", "merge-base", "--is-ancestor", mine, "HEAD"],
            cwd=self.me).returncode, 0)
        self.assertEqual(self.read("c.txt"), "new\n")

    def test_another_remote_means_merge(self):
        # the commit may sit on a fork this clone never fetched: no rewrite
        fork = os.path.join(os.path.dirname(self.up), "fork")
        git(os.path.dirname(self.up), "init", "-q", "--bare", fork)
        git(self.me, "remote", "add", "fork", fork)
        self.write(self.me, "b.txt", "mine\n")
        mine = self.commit(self.me, "mine")
        self.new_upstream()
        self.pull()
        self.assertEqual(subprocess.run(
            ["git", "merge-base", "--is-ancestor", mine, "HEAD"],
            cwd=self.me).returncode, 0)
        self.assertEqual(self.read("c.txt"), "new\n")

    def test_clash_leaves_the_folder_as_it_was(self):
        self.write(self.me, "a.txt", "mine\n")
        mine = self.commit(self.me, "mine")
        self.write(self.up, "a.txt", "theirs\n")
        self.commit(self.up, "theirs")
        with self.assertRaises(RuntimeError) as cm:
            self.pull()
        self.assertIn("git pull --rebase origin main", str(cm.exception))
        self.assertEqual(git(self.me, "rev-parse", "HEAD"), mine)
        self.assertEqual(git(self.me, "status", "--porcelain"), "")
        self.assertFalse(os.path.exists(os.path.join(self.me, ".git", "rebase-merge")))

    def test_clashing_uncommitted_change_is_put_back(self):
        # git exits 0 here and leaves conflict markers: Update must not
        # go on and install that
        self.write(self.me, "a.txt", "editing\n")
        before = git(self.me, "rev-parse", "HEAD")
        self.write(self.up, "a.txt", "theirs\n")
        self.commit(self.up, "release")
        with self.assertRaises(RuntimeError):
            self.pull()
        self.assertEqual(git(self.me, "rev-parse", "HEAD"), before)
        self.assertEqual(self.read("a.txt"), "editing\n")
        self.assertEqual(git(self.me, "stash", "list"), "")

    def test_clash_with_commit_and_uncommitted_change(self):
        self.write(self.me, "b.txt", "my fix\n")
        mine = self.commit(self.me, "my fix")
        self.write(self.me, "a.txt", "editing\n")
        self.write(self.up, "a.txt", "theirs\n")
        self.commit(self.up, "release")
        with self.assertRaises(RuntimeError):
            self.pull()
        self.assertEqual(git(self.me, "rev-parse", "HEAD"), mine)
        self.assertEqual(self.read("a.txt"), "editing\n")
        self.assertEqual(git(self.me, "stash", "list"), "")

    def test_other_branch_is_left_alone(self):
        git(self.me, "checkout", "-q", "-b", "work")
        here = git(self.me, "rev-parse", "HEAD")
        self.new_upstream()
        with self.assertRaises(tabit._LeaveCheckout) as cm:
            self.pull()
        self.assertIn("on branch work", str(cm.exception))
        self.assertEqual(git(self.me, "rev-parse", "HEAD"), here)

    def test_detached_head_is_left_alone(self):
        git(self.me, "checkout", "-q", "--detach")
        self.new_upstream()
        with self.assertRaises(tabit._LeaveCheckout) as cm:
            self.pull()
        self.assertIn("on no branch", str(cm.exception))

    def test_rebase_in_progress_is_left_alone(self):
        # a rebase stopped on a conflict, half resolved: never abort it
        self.write(self.me, "a.txt", "mine\n")
        self.commit(self.me, "mine")
        self.write(self.up, "a.txt", "theirs\n")
        self.commit(self.up, "theirs")
        git(self.me, "fetch", "-q", "origin")
        subprocess.run(["git", "rebase", "origin/main"], cwd=self.me, capture_output=True)
        self.write(self.me, "a.txt", "resolved by hand\n")
        with self.assertRaises(tabit._LeaveCheckout) as cm:
            self.pull()
        self.assertIn("middle of a git operation", str(cm.exception))
        self.assertTrue(os.path.isdir(os.path.join(self.me, ".git", "rebase-merge")))
        self.assertEqual(self.read("a.txt"), "resolved by hand\n")

    def test_merge_path_says_merge_by_hand(self):
        fork = os.path.join(os.path.dirname(self.up), "fork")
        git(os.path.dirname(self.up), "init", "-q", "--bare", fork)
        git(self.me, "remote", "add", "fork", fork)
        self.write(self.me, "a.txt", "mine\n")
        self.commit(self.me, "mine")
        self.write(self.up, "a.txt", "theirs\n")
        self.commit(self.up, "theirs")
        with self.assertRaises(RuntimeError) as cm:
            self.pull()
        self.assertIn("git pull --no-rebase origin main", str(cm.exception))

    def source(self, repo):
        fetched = []

        def fetch(ver, log):
            fetched.append(ver)
            return "/tmp/release"
        got = tabit._update_source_dir(repo, "v9.9.9", self.log.append,
                                       lambda *_a: None, fetch)
        return got, fetched

    def test_source_is_the_checkout_on_main(self):
        self.new_upstream()
        self.assertEqual(self.source(self.me), (self.me, []))

    def test_source_is_the_release_for_a_work_branch(self):
        # the developer's own branch: left as it is, the release installs
        git(self.me, "checkout", "-q", "-b", "work")
        here = git(self.me, "rev-parse", "HEAD")
        self.new_upstream()
        self.assertEqual(self.source(self.me), ("/tmp/release", ["v9.9.9"]))
        self.assertEqual(git(self.me, "rev-parse", "HEAD"), here)
        self.assertEqual(git(self.me, "rev-parse", "--abbrev-ref", "HEAD"), "work")

    def test_source_is_the_release_with_no_checkout(self):
        self.assertEqual(self.source(None), ("/tmp/release", ["v9.9.9"]))

    def test_a_clash_is_not_answered_with_the_release(self):
        # the user's own changes are involved: stop and say so
        self.write(self.me, "a.txt", "mine\n")
        self.commit(self.me, "mine")
        self.write(self.up, "a.txt", "theirs\n")
        self.commit(self.up, "theirs")
        with self.assertRaises(RuntimeError):
            self.source(self.me)

    def test_no_network(self):
        git(self.me, "remote", "set-url", "origin", "/nonexistent/repo")
        with self.assertRaises(RuntimeError) as cm:
            self.pull()
        self.assertIn("fetch", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
