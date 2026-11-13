#!/usr/bin/env python3
"""Tabit Chat: raw claude events -> chat events, one chat, and groups.

A fake claude (tests/fake_claude.py) stands in for the real one. No GTK here.
"""
import json
import os
import shutil
import sys
import tempfile
import threading
import subprocess
import time
import unittest
import urllib.parse
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import tabit_chat as tc  # noqa: E402

FAKE = os.path.join(HERE, "fake_claude.py")


def wait(cond, secs=8):
    end = time.time() + secs
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.03)
    return False


class TestTranslate(unittest.TestCase):
    def setUp(self):
        with open(os.path.join(HERE, "fixtures", "chat", "real_write_allowed.jsonl")) as f:
            self.raw = [json.loads(line) for line in f]

    def test_real_stream_gives_text_tool_result_done(self):
        kinds = [e["t"] for r in self.raw for e in tc.translate(r)]
        self.assertIn("tool", kinds)
        self.assertIn("result", kinds)
        self.assertIn("text", kinds)
        self.assertEqual(kinds[-1], "done")
        self.assertIn("delta", kinds)

    def test_real_stream_has_one_permission_question(self):
        asks = [a for a in map(tc.ask_of, self.raw) if a]
        self.assertEqual(len(asks), 1)
        rid, tool, inp, suggest, use_id = asks[0]
        self.assertEqual(tool, "Write")
        self.assertIn("file_path", inp)
        self.assertTrue(use_id)

    def test_thinking_is_not_shown(self):
        for r in self.raw:
            for e in tc.translate(r):
                self.assertNotIn("thinking", json.dumps(e))

    def test_sub_agent_lines_are_dropped(self):
        raw = {"type": "assistant", "parent_tool_use_id": "x",
               "message": {"content": [{"type": "text", "text": "hi"}]}}
        self.assertEqual(tc.translate(raw), [])

    def test_tool_result_blocks_flatten(self):
        raw = {"type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": "t", "is_error": True,
             "content": [{"type": "text", "text": "a"}, {"type": "text", "text": "b"}]}]}}
        self.assertEqual(tc.translate(raw)[0]["text"], "a\nb")
        self.assertTrue(tc.translate(raw)[0]["error"])

    def test_junk_is_ignored(self):
        for junk in (None, 3, [], {}, {"type": "stream_event"}):
            self.assertEqual(tc.translate(junk), [])
        self.assertIsNone(tc.ask_of({"type": "control_request", "request": {}}))


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = self.tmp.name
        self.log = os.path.join(self.dir, "argv.log")
        os.environ["FAKE_CLAUDE_LOG"] = self.log
        self.mgr = self.make()
        self.addCleanup(self.stop_all)
        self.addCleanup(self.tmp.cleanup)

    def make(self):
        return tc.ChatManager(root=os.path.join(self.dir, "chats"), claude=FAKE,
                              env_drop=("CLAUDECODE",))

    def stop_all(self):
        self.mgr.stop_mirrors()
        for c in self.mgr.chats.values():
            c.stop_process()

    def new(self, **kw):
        code, out = self.mgr.create(dict({"cwd": self.dir}, **kw))
        self.assertEqual(code, 200, out)
        return self.mgr.get(out["chat"]["id"])

    def argvs(self):
        with open(self.log) as f:
            return [json.loads(line) for line in f]

    def kinds(self, chat):
        return [e["t"] for e in chat.events]


class LibraryTest(Base):
    def test_library_lists_written_files_newest_first_without_repeats(self):
        chat = self.new()
        for name in ("a.txt", "b.txt", "a.txt"):
            chat.emit({"t": "tool", "id": name, "name": "Write",
                       "detail": json.dumps({"file_path": "/x/" + name})})
        chat.emit({"t": "tool", "id": "r", "name": "Read", "detail": json.dumps({"file_path": "/x/c.txt"})})
        chat.emit({"t": "done", "cost": 0.25})
        lib = chat.library()
        self.assertEqual([f["name"] for f in lib["files"]], ["a.txt", "b.txt"])
        self.assertEqual((lib["turns"], lib["cost"]), (1, 0.25))
        code, out = tc.api(self.mgr, "GET", [chat.id, "library"], {})
        self.assertEqual((code, len(out["files"])), (200, 2))


class DefaultChatTest(Base):
    def test_the_old_main_chat_leaves_with_its_history_kept(self):
        main = self.new(title="Tabit Chat")
        main.meta["default"] = True
        main._save_meta()
        mine = self.new(title="Later")
        self.mgr.retire_default()
        self.assertEqual([r["title"] for r in self.mgr.listing()], ["Later"])
        self.assertTrue(os.path.isdir(main.dir + ".retired"))
        self.stop_all()
        again = self.make()                       # not back after a restart
        self.assertEqual(list(again.chats), [mine.id])
        again.stop_mirrors()


class AccountTest(Base):
    def make(self):
        return tc.ChatManager(root=os.path.join(self.dir, "chats"), claude=FAKE,
                              env_drop=("CLAUDECODE",),
                              accounts=lambda: [("Work", FAKE)],
                              auth=lambda cli: {"loggedIn": True, "email": "a@b.c", "subscriptionType": "pro"})

    def test_the_logins_have_labels(self):
        rows = self.mgr.account_choices()
        self.assertEqual([r["name"] for r in rows], ["", "Work"])
        self.assertEqual(rows[1]["label"], "Work — a@b.c · pro")

    def test_a_chat_picks_its_own_login(self):
        a, b = self.new(), self.new()
        self.assertEqual(tc.api(self.mgr, "POST", [a.id, "edit"], {"account": "nobody"})[0], 400)
        self.assertEqual(tc.api(self.mgr, "POST", [a.id, "edit"], {"account": "Work"})[0], 200)
        self.assertEqual((a.summary()["account"], b.summary()["account"]), ("Work", ""))
        self.assertEqual(tc.api(self.mgr, "POST", ["account"], {"account": "Work"})[0], 404)   # no login for all


class SafetyTest(Base):

    def test_meta_written_from_many_threads_at_once(self):
        chat = self.new()
        errors = []

        def save():
            try:
                for _ in range(30):
                    chat._save_meta()
            except Exception as e:
                errors.append(e)
        ts = [threading.Thread(target=save) for _ in range(6)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        self.assertEqual(errors, [])
        with open(os.path.join(chat.dir, "meta.json")) as f:
            self.assertEqual(json.load(f)["id"], chat.id)
        self.assertEqual([n for n in os.listdir(chat.dir) if n.endswith(".tmp")], [])

    def test_a_dead_claude_is_not_written_to(self):
        chat = self.new()

        class Gone:
            def write(self, _s):
                raise BrokenPipeError(32, "Broken pipe")
            flush = write
        chat.proc = type("P", (), {"stdin": Gone(), "poll": lambda self: None})()
        self.assertFalse(chat.interrupt())
        chat.pending["r1"] = {"ev": {"t": "ask"}, "input": {}, "suggest": None}
        self.assertEqual(tc.api(self.mgr, "POST", [chat.id, "answer"],
                                {"rid": "r1", "allow": True})[0], 409)
        chat.proc = None

    def test_a_long_wait_for_send_to_tab_is_not_cut(self):
        # claude's Bash would kill tabit-tabs after 2 minutes while the
        # "Send to tab" card still waits for the user
        env = self.mgr.child_env(self.new())
        self.assertGreaterEqual(int(env["BASH_DEFAULT_TIMEOUT_MS"]), tc.ASK_WAIT * 1000)


class LookTest(Base):
    def test_pick_an_animal_and_colour_when_making_and_editing(self):
        code, out = self.mgr.create({"cwd": self.dir, "emoji": "🐙", "hue": 200})
        self.assertEqual((code, out["chat"]["emoji"], out["chat"]["hue"]), (200, "🐙", 200))
        chat = self.mgr.get(out["chat"]["id"])
        code, out = tc.api(self.mgr, "POST", [chat.id, "edit"], {"emoji": "🦊", "hue": None})
        self.assertEqual((code, out["chat"]["emoji"], out["chat"]["hue"]), (200, "🦊", None))
        self.assertEqual(tc.api(self.mgr, "POST", [chat.id, "edit"], {"emoji": "abc"})[0], 400)
        self.assertEqual(tc.api(self.mgr, "POST", [chat.id, "edit"], {"hue": 360})[0], 400)
        self.assertEqual(self.mgr.create({"cwd": self.dir, "hue": True})[0], 400)


class TestChat(Base):
    def test_send_gets_a_reply(self):
        chat = self.new()
        self.assertEqual(tc.api(self.mgr, "POST", [chat.id, "send"], {"text": "hello"})[0], 200)
        self.assertTrue(wait(lambda: "done" in self.kinds(chat)))
        self.assertEqual(self.kinds(chat), ["user", "text", "done"])
        self.assertEqual(chat.events[1]["text"], "echo: hello")
        self.assertEqual(chat.state, "idle")
        self.assertEqual(chat.meta["title"], "hello")        # the first message names it

    def test_argv_asks_by_default_and_starts_a_new_session(self):
        chat = self.new()
        chat.send("hi")
        wait(lambda: "done" in self.kinds(chat))
        argv = self.argvs()[0]
        self.assertEqual(argv[argv.index("--permission-mode") + 1], "default")
        self.assertEqual(argv[argv.index("--permission-prompt-tool") + 1], "stdio")
        self.assertIn("--session-id", argv)
        self.assertNotIn("--allow-dangerously-skip-permissions", argv)

    def test_a_picked_effort_goes_on_the_command(self):
        chat = self.new()
        self.assertNotIn("--effort", chat._argv())
        self.assertEqual(tc.api(self.mgr, "POST", [chat.id, "edit"], {"effort": "ultra"})[0], 400)
        self.assertEqual(tc.api(self.mgr, "POST", [chat.id, "edit"], {"effort": "high"})[0], 200)
        argv = chat._argv()
        self.assertEqual(argv[argv.index("--effort") + 1], "high")
        self.assertEqual(chat.summary()["effort"], "high")

    def test_allow_and_deny(self):
        chat = self.new()
        chat.send("write a.txt")
        self.assertTrue(wait(lambda: chat.pending))
        self.assertEqual(chat.state, "waiting")
        ask = chat.view()["pending"][0]
        self.assertEqual((ask["tool"], ask["summary"]), ("Write", "/x/a.txt"))
        self.assertTrue(ask["can_remember"])
        r = tc.api(self.mgr, "POST", [chat.id, "answer"], {"rid": ask["rid"], "allow": True})
        self.assertEqual(r[0], 200)
        self.assertTrue(wait(lambda: "done" in self.kinds(chat)))
        self.assertEqual([e for e in chat.events if e["t"] == "text"][-1]["text"], "wrote")
        # answering again: the question is gone
        self.assertEqual(tc.api(self.mgr, "POST", [chat.id, "answer"],
                                {"rid": ask["rid"], "allow": True})[0], 404)
        chat.send("write b.txt")
        self.assertTrue(wait(lambda: chat.pending))
        chat.answer("rq1", False, "not now")
        self.assertTrue(wait(lambda: self.kinds(chat).count("done") == 2))
        self.assertEqual([e for e in chat.events if e["t"] == "text"][-1]["text"],
                         "denied: not now")

    def test_stop_ends_the_turn_and_the_chat_goes_on(self):
        chat = self.new()
        chat.send("slow")
        self.assertTrue(wait(lambda: chat.live))
        self.assertEqual(chat.state, "working")
        self.assertTrue(chat.interrupt())
        self.assertTrue(wait(lambda: "done" in self.kinds(chat)))
        self.assertEqual(chat.live, "")
        chat.send("say alive")
        self.assertTrue(wait(lambda: self.kinds(chat).count("done") == 2))

    def test_a_crash_says_why_and_the_next_message_resumes(self):
        chat = self.new()
        chat.send("hi")
        wait(lambda: "done" in self.kinds(chat))
        chat.send("die")
        self.assertTrue(wait(lambda: "error" in self.kinds(chat)))
        err = [e for e in chat.events if e["t"] == "error"][0]["text"]
        self.assertIn("boom: it died", err)
        self.assertEqual(chat.state, "idle")
        chat.send("say back")
        self.assertTrue(wait(lambda: self.kinds(chat).count("done") == 2))
        argv = self.argvs()[-1]
        self.assertIn("--resume", argv)
        self.assertEqual(argv[argv.index("--resume") + 1], chat.meta["session"])

    def test_mode_change_restarts_only_when_idle(self):
        chat = self.new()
        chat.send("slow")
        wait(lambda: chat.live)
        self.assertEqual(tc.api(self.mgr, "POST", [chat.id, "edit"], {"mode": "edits"})[0], 409)
        chat.interrupt()
        wait(lambda: chat.state == "idle")
        self.assertEqual(tc.api(self.mgr, "POST", [chat.id, "edit"], {"mode": "edits"})[0], 200)
        self.assertTrue(wait(lambda: not chat.running))
        chat.send("hi")
        wait(lambda: self.kinds(chat).count("done") == 2)
        argv = self.argvs()[-1]
        self.assertEqual(argv[argv.index("--permission-mode") + 1], "acceptEdits")

    def test_reload_ends_the_process_and_keeps_the_session(self):
        chat = self.new()
        chat.send("hi")
        wait(lambda: "done" in self.kinds(chat))
        session = chat.meta["session"]
        self.assertTrue(chat.running)
        self.assertEqual(tc.api(self.mgr, "POST", [chat.id, "reload"], {})[0], 200)
        self.assertTrue(wait(lambda: not chat.running))
        chat.send("again")
        wait(lambda: self.kinds(chat).count("done") == 2)
        argv = self.argvs()[-1]
        self.assertEqual(argv[argv.index("--resume") + 1], session)

    def test_reload_waits_for_an_idle_chat(self):
        chat = self.new()
        chat.send("slow")
        wait(lambda: chat.live)
        self.assertEqual(tc.api(self.mgr, "POST", [chat.id, "reload"], {})[0], 409)
        chat.interrupt()

    def test_bypass_is_asked_for_by_name_only(self):
        chat = self.new(mode="bypass")
        chat.send("hi")
        wait(lambda: "done" in self.kinds(chat))
        argv = self.argvs()[0]
        self.assertEqual(argv[argv.index("--permission-mode") + 1], "bypassPermissions")
        self.assertIn("--allow-dangerously-skip-permissions", argv)

    def test_chats_come_back_after_a_restart(self):
        chat = self.new()
        chat.send("say remember me")
        wait(lambda: "done" in self.kinds(chat))
        chat.stop_process()
        again = self.make()
        back = again.get(chat.id)
        self.assertEqual([e["t"] for e in back.events], ["user", "text", "done"])
        self.assertEqual(back.seq, chat.seq)
        self.assertTrue(back.meta["started"])
        back.send("hi")                                     # resumes, same session
        wait(lambda: [e["t"] for e in back.events].count("done") == 2)
        self.assertIn("--resume", self.argvs()[-1])
        back.stop_process()

    def test_bad_input(self):
        self.assertEqual(self.mgr.create({"cwd": "/no/such/dir"})[0], 400)
        self.assertEqual(self.mgr.create({"cwd": self.dir, "mode": "x"})[0], 400)
        self.assertEqual(self.mgr.create({"cwd": self.dir, "model": "gpt"})[0], 400)
        chat = self.new()
        self.assertEqual(tc.api(self.mgr, "POST", [chat.id, "send"], {"text": "  "})[0], 400)
        self.assertEqual(tc.api(self.mgr, "POST", [chat.id, "send"], {"text": "x" * 70000})[0], 400)
        self.assertEqual(tc.api(self.mgr, "GET", ["nope"], {})[0], 404)
        self.assertEqual(tc.api(self.mgr, "GET", [chat.id, "send"], {})[0], 405)

    def test_delete_removes_it(self):
        chat = self.new()
        chat.send("hi")
        wait(lambda: "done" in self.kinds(chat))
        self.assertEqual(tc.api(self.mgr, "POST", [chat.id, "delete"], {})[0], 200)
        self.assertIsNone(self.mgr.get(chat.id))
        self.assertFalse(os.path.exists(chat.dir))
        self.assertTrue(wait(lambda: not chat.running))


class TestStream(Base):
    def test_a_page_that_comes_back_gets_what_it_missed(self):
        chat = self.new()
        chat.send("say one")
        wait(lambda: "done" in self.kinds(chat))
        pushes, stop, kick = [], threading.Event(), threading.Event()

        def write(b):
            pushes.append(b.decode())
            if len(pushes) >= 2:
                stop.set()

        t = threading.Thread(target=tc.stream, args=(chat, 1, write, stop, kick), daemon=True)
        t.start()
        wait(lambda: pushes)
        chat.send("say two")                               # while the page watches
        t.join(8)
        data = json.loads(pushes[0].split("data: ", 1)[1])
        self.assertEqual([e["t"] for e in data["events"]], ["text", "done"])  # after seq 1
        self.assertIn("id: ", pushes[0])
        self.assertEqual(data["state"], "idle")
        later = json.loads(pushes[1].split("data: ", 1)[1])
        self.assertEqual((later["events"][0]["t"], later["events"][0]["text"]), ("user", "say two"))

    def test_no_event_is_lost_while_they_pour_in(self):
        chat = self.new()
        got, stop, kick = [], threading.Event(), threading.Event()

        def write(b):
            data = json.loads(b.decode().split("data: ", 1)[1])
            got.extend(e["seq"] for e in data["events"])
            if data["seq"] >= 400:
                stop.set()

        t = threading.Thread(target=tc.stream, args=(chat, 0, write, stop, kick), daemon=True)
        t.start()
        for i in range(400):
            chat.emit({"t": "text", "text": str(i)})
        t.join(10)
        self.assertEqual(got, list(range(1, 401)))          # every one, once, in order

    def test_tail_limits_the_first_push_and_says_there_is_more(self):
        chat = self.new()
        for i in range(3):
            chat.send("say n%d" % i)
            wait(lambda: self.kinds(chat).count("done") == i + 1)
        pushes, stop = [], threading.Event()
        stop.set()
        tc.stream(chat, 0, lambda b: pushes.append(b.decode()), stop, threading.Event(), tail=2)
        self.assertEqual(pushes, [])                       # stop is checked first

        stop2 = threading.Event()

        def write(b):
            pushes.append(b.decode())
            stop2.set()
        tc.stream(chat, 0, write, stop2, threading.Event(), tail=2)
        data = json.loads(pushes[0].split("data: ", 1)[1])
        self.assertEqual(len(data["events"]), 2)
        self.assertTrue(data["older"])


class TestGroup(Base):
    def group(self, n=2):
        chats = [self.new(title="Alice" if i == 0 else "Bob" if i == 1 else "Cy")
                 for i in range(n)]
        code, out = self.mgr.create_group({"title": "Team", "members": [c.id for c in chats]})
        self.assertEqual(code, 200, out)
        return self.mgr.groups[out["group"]["id"]], chats

    def says(self, group):
        return [(e["name"], e["text"]) for e in group.events if e["t"] == "say"]

    def test_the_user_talks_to_everyone(self):
        group, (a, b) = self.group()
        group.send("say hi all")
        self.assertTrue(wait(lambda: len(self.says(group)) == 2))
        self.assertEqual(sorted(n for n, _ in self.says(group)), ["alice", "bob"])
        user = [e for e in group.events if e["t"] == "user"][0]
        self.assertEqual(sorted(user["to"]), ["alice", "bob"])

    def test_at_name_talks_to_one(self):
        group, (a, b) = self.group()
        group.send("@bob say only you")
        self.assertTrue(wait(lambda: self.says(group)))
        time.sleep(0.3)
        self.assertEqual(self.says(group), [("bob", "only you")])
        self.assertEqual(a.events, [])

    def test_several_at_names_talk_to_those_only(self):
        group, (a, b, c) = self.group(3)
        group.send("@alice @cy say just us")
        self.assertTrue(wait(lambda: len(self.says(group)) == 2))
        time.sleep(0.3)
        self.assertEqual(sorted(n for n, _ in self.says(group)), ["alice", "cy"])
        self.assertEqual(b.events, [])

    def test_several_at_names_with_commas_talk_to_those_only(self):
        group, (a, b, c) = self.group(3)
        group.send("@alice, @cy, say just us")
        self.assertTrue(wait(lambda: len(self.says(group)) == 2))
        time.sleep(0.3)
        self.assertEqual(sorted(n for n, _ in self.says(group)), ["alice", "cy"])
        self.assertEqual(b.events, [])

    def test_a_reply_can_name_several_chats(self):
        group, (a, b, c) = self.group(3)
        self.assertEqual([x.id for x in group._mentions("@bob, @cy: are you there", skip=a)], [b.id, c.id])

    def test_what_a_member_is_asked_in_its_own_chat_stays_there(self):
        group, (a, b) = self.group()
        a.send("say @bob: a private answer")
        self.assertTrue(wait(lambda: any(e["t"] == "text" for e in a.events)))
        time.sleep(0.3)
        self.assertEqual(self.says(group), [])
        self.assertFalse(any(e["t"] == "relay" for e in group.events))
        self.assertEqual(b.events, [])
        group.send("@alice say @bob: for the group")           # the group's own question
        self.assertTrue(wait(lambda: any(e["t"] == "user" for e in b.events)))
        self.assertEqual(self.says(group)[0], ("alice", "@bob: for the group"))
        self.assertTrue(wait(lambda: any(e["t"] == "text" for e in b.events)))   # bob is done

    def test_a_member_writing_now_shows_only_for_the_group(self):
        group, (a, b) = self.group()
        a.owners.append(None)                               # answering its own chat
        a.live = "half an answer"
        self.assertEqual(group.view()["live"], {})
        a.owners.clear()
        a.owners.append(group.id)                           # answering the group
        self.assertEqual(group.view()["live"], {a.id: "half an answer"})

    def test_a_private_line_during_a_group_turn_leaves_the_group_its_answer(self):
        group, (a, b) = self.group()
        group.send("@alice later for the group")
        self.assertTrue(wait(lambda: a.inflight))
        a.send("say just for me")                           # while alice still works
        self.assertTrue(wait(lambda: len([e for e in a.events if e["t"] == "text"]) == 2))
        time.sleep(0.2)
        self.assertEqual(self.says(group), [("alice", "for the group")])

    def test_a_group_line_during_a_private_turn_does_not_take_its_answer(self):
        group, (a, b) = self.group()
        a.send("later @bob: a secret")
        group.send("@alice say hi all")                     # while alice still works
        self.assertTrue(wait(lambda: len([e for e in a.events if e["t"] == "text"]) == 2))
        time.sleep(0.2)
        self.assertEqual(self.says(group), [("alice", "hi all")])
        self.assertEqual(b.events, [])

    def test_a_crash_leaves_no_owner_for_the_next_claude(self):
        group, (a, b) = self.group()
        real = tc.Chat._died

        def late(chat, proc):
            time.sleep(0.5)                                 # the reader is a little late
            return real(chat, proc)
        with mock.patch.object(tc.Chat, "_died", late):
            group.send("@alice die")
            self.assertTrue(wait(lambda: a.proc is not None and a.proc.poll() is not None))
            a.send("say private only")                      # a new claude starts at once
            self.assertTrue(wait(lambda: any(e["t"] == "text" for e in a.events)))
            time.sleep(0.8)
        self.assertEqual(self.says(group), [])
        self.assertEqual((list(a.owners), a.inflight), ([], 0))

    def test_a_private_message_that_did_not_go_stays_out_of_the_group(self):
        group, (a, b) = self.group()
        a.owners.append(group.id)                           # the group's question is being answered
        a.emit({"t": "error", "text": "Not sent: x", "unsent": True})
        self.assertFalse(any(e["t"] == "error" for e in group.events))

    def test_a_message_that_cannot_reach_claude_says_so(self):
        group, (a, b) = self.group()
        a.send("say up")
        self.assertTrue(wait(lambda: a.state == "idle" and a.events[-1]["t"] == "done"))
        with mock.patch.object(tc.Chat, "_write", side_effect=RuntimeError("Claude has stopped")):
            with self.assertRaises(RuntimeError):
                a.send("hello")
        self.assertEqual([(e["t"], e["text"]) for e in a.events[-2:]],
                         [("user", "hello"), ("error", "Not sent: Claude has stopped")])
        self.assertEqual((a.inflight, list(a.owners)), (0, []))

    def test_members_can_join_and_leave(self):
        group, (a, b, c) = self.group(3)
        d = self.new(title="Dee")
        code, out = tc.group_api(self.mgr, "POST", [group.id, "members"], {"add": [d.id], "remove": [a.id]})
        self.assertEqual(code, 200, out)
        self.assertEqual([m["alias"] for m in out["group"]["members"]], ["bob", "cy", "dee"])
        group.send("@dee hello")
        self.assertTrue(wait(lambda: self.says(group)))
        self.assertEqual(a.events, [])                      # alice is out
        notes = [e["text"] for e in group.events if e["t"] == "notice"]
        self.assertIn("alice was taken out of the group.", notes)
        self.assertIn("dee joined the group.", notes)
        got = [e["text"] for e in d.events if e["t"] == "user"][0]
        self.assertIn("bob, cy, dee", got)                  # briefed with the new room
        self.assertEqual(tc.group_api(self.mgr, "POST", [group.id, "members"], {"remove": [b.id, c.id]})[0], 400)

    def test_a_deleted_chat_leaves_its_groups(self):
        group, (a, b, c) = self.group(3)
        self.mgr.delete(b)
        self.assertEqual(group.meta["members"], [a.id, c.id])
        self.assertNotIn(b.id, group.meta["alias"])
        self.assertTrue(any("bob was deleted" in e["text"] for e in group.events if e["t"] == "notice"))
        self.assertEqual(len(group.summary()["members"]), 2)

    def test_a_member_says_which_ai_it_is(self):
        # the page colors a Codex member's words like Codex's terminal
        group, (a, b) = self.group(2)
        b.meta["tab"] = {"cli": "codex"}
        self.assertEqual([m["cli"] for m in group.summary()["members"]], ["claude", "codex"])

    def test_a_chat_can_pass_a_message_to_another(self):
        group, (a, b) = self.group()
        # bob is told to say a line that starts with @alice: it reaches alice
        group.send("@bob say @alice: are you there")
        self.assertTrue(wait(lambda: any(e["t"] == "relay" for e in group.events)))
        self.assertTrue(wait(lambda: any(n == "alice" for n, _ in self.says(group))))
        relay = [e for e in group.events if e["t"] == "relay"][0]
        self.assertEqual((relay["from"], relay["to"]), ("bob", "alice"))
        got = [e["text"] for e in a.events if e["t"] == "user"][0]
        self.assertIn("[bob]: @alice: are you there", got)
        self.assertIn('group chat called "Team"', got)      # told how the group works, once

    def test_two_chats_cannot_talk_forever(self):
        group, (a, b) = self.group()
        group.send("@alice bounce bob alice")
        self.assertTrue(wait(lambda: group.paused, 10))
        notice = [e for e in group.events if e["t"] == "notice"]
        self.assertEqual(len(notice), 1)
        self.assertLessEqual(len([e for e in group.events if e["t"] == "relay"]), tc.MAX_HOPS)
        time.sleep(0.5)
        n = len(group.events)
        time.sleep(0.5)
        self.assertEqual(len(group.events), n)             # it has really stopped
        group.send("say go on")                            # the user restarts it
        self.assertFalse(group.paused)
        self.assertTrue(wait(lambda: [t for _, t in self.says(group)].count("go on") == 2))

    def test_the_group_shows_every_members_open_question(self):
        group, (a, b) = self.group()
        group.send("@bob write z.txt")
        self.assertTrue(wait(lambda: group.view()["pending"]))
        ask = group.view()["pending"][0]
        self.assertEqual((ask["chat"], ask["name"]), (b.id, "bob"))
        self.assertEqual(group.state, "waiting")
        self.assertTrue(group.answer(b.id, ask["rid"], True))
        self.assertTrue(wait(lambda: ("bob", "wrote") in self.says(group)))

    def test_group_stop_and_routes(self):
        group, (a, b) = self.group()
        self.assertEqual(tc.group_api(self.mgr, "POST", [group.id, "send"], {"text": "slow"})[0], 200)
        self.assertTrue(wait(lambda: a.live and b.live))
        self.assertEqual(sorted(group.view()["live"]), sorted([a.id, b.id]))
        tc.group_api(self.mgr, "POST", [group.id, "stop"], {})
        self.assertTrue(wait(lambda: group.state == "idle"))
        self.assertEqual(self.mgr.create_group({"members": [a.id]})[0], 400)
        self.assertEqual(self.mgr.create_group({"members": [a.id, "zzz"]})[0], 400)
        self.assertEqual(tc.group_api(self.mgr, "POST", [group.id, "delete"], {})[0], 200)
        self.assertEqual(self.mgr.groups, {})
        self.assertIn(a.id, self.mgr.chats)                # the chats stay

    def test_same_title_gets_different_names(self):
        a, b = self.new(title="Dev"), self.new(title="Dev")
        code, out = self.mgr.create_group({"members": [a.id, b.id]})
        names = [m["alias"] for m in out["group"]["members"]]
        self.assertEqual(sorted(names), ["dev", "dev-2"])

    def test_a_long_title_keeps_its_whole_name(self):
        a, b = self.new(title="new-marathon-planner"), self.new(title="x" * 60)
        code, out = self.mgr.create_group({"members": [a.id, b.id]})
        names = [m["alias"] for m in out["group"]["members"]]
        self.assertEqual(names, ["new-marathon-planner", "x" * 40])

    def test_an_old_cut_name_is_made_whole_when_the_group_loads(self):
        a, b = self.new(title="new-marathon-planner"), self.new(title="gogo")
        code, out = self.mgr.create_group({"members": [a.id, b.id]})
        gid = out["group"]["id"]
        group = self.mgr.groups[gid]
        group.meta["alias"][a.id] = "new-marathon-pla"      # as an old version saved it
        group.meta["briefed"] = [a.id]
        group._save_meta()
        self.stop_all()
        back = self.make().groups[gid]
        self.assertEqual(back.meta["alias"][a.id], "new-marathon-planner")
        self.assertEqual(back.meta["alias"][b.id], "gogo")
        self.assertEqual(back.meta["briefed"], [])

    def test_groups_come_back_after_a_restart(self):
        group, (a, b) = self.group()
        group.send("say hi")
        wait(lambda: len(self.says(group)) == 2)
        self.stop_all()
        again = self.make()
        back = again.groups[group.id]
        self.assertEqual(len(back.members()), 2)
        self.assertEqual(len([e for e in back.events if e["t"] == "say"]), 2)


class TabToolTest(Base):
    def hooked(self):
        self.sent = []

        def hook(tab, text=None):
            if tab != "t1":
                return {"error": "that is not an AI tab"}
            if text is None:
                return {"title": "Dev", "status": "idle"}
            self.sent.append(text)
            return 200, {"ok": True}
        self.mgr.tab_hook = hook

    def ask_and_answer(self, chat, allow):
        out = []
        t = threading.Thread(target=lambda: out.append(
            tc.api(self.mgr, "POST", [chat.id, "tabs", "t1", "send"], {"text": "go"})))
        t.start()
        wait(lambda: chat.pending)
        rid = next(iter(chat.pending))
        self.assertTrue(chat.answer(rid, allow))
        t.join(5)
        return out[0]

    def test_send_waits_for_a_yes(self):
        self.hooked()
        chat = self.new()
        self.assertEqual(self.ask_and_answer(chat, True), (200, {"ok": True}))
        self.assertEqual(self.sent, ["go"])
        self.assertEqual(chat.pending, {})

    def test_a_no_types_nothing(self):
        self.hooked()
        chat = self.new()
        code, out = self.ask_and_answer(chat, False)
        self.assertEqual(code, 403)
        self.assertEqual(self.sent, [])

    def test_bad_tab_text_and_no_hook(self):
        chat = self.new()
        self.assertEqual(tc.api(self.mgr, "POST", [chat.id, "tabs", "t1", "send"],
                                {"text": "x"})[0], 404)
        self.hooked()
        self.assertEqual(tc.api(self.mgr, "POST", [chat.id, "tabs", "zz", "send"],
                                {"text": "x"})[0], 409)
        self.assertEqual(tc.api(self.mgr, "POST", [chat.id, "tabs", "t1", "send"],
                                {"text": " "})[0], 400)

    def test_child_env_and_allowed_tool(self):
        self.mgr.extra_env = lambda: {"TABIT_SOCK": "/x"}
        chat = self.new()
        env = self.mgr.child_env(chat)
        self.assertEqual(env["TABIT_CHAT_ID"], chat.id)
        self.assertEqual(env["TABIT_SOCK"], "/x")
        argv = chat._argv()
        self.assertIn("Bash(%s:*)" % tc.TABS_TOOL, argv)


class MirrorTest(Base):
    """A chat that mirrors an AI tab: its bubbles come from the tab's transcript."""

    def setUp(self):
        super().setUp()
        self.addCleanup(setattr, tc, "MIRROR_EVERY", tc.MIRROR_EVERY)
        tc.MIRROR_EVERY = 3600       # the tests look by hand
        self.path = os.path.join(self.dir, "tx.jsonl")
        self.status = "idle"
        self.calls = []
        shutil.copy(os.path.join(os.path.dirname(__file__), "fixtures", "chat",
                                 "tab_transcript.jsonl"), self.path)

        def hook(tab, text=None, key=None, queue=False, screen=False):
            if tab != "t1":
                return (404, {"error": "no"}) if text or key else {"error": "no"}
            if text is None and key is None:
                return {"title": "MOT", "status": self.status,
                        "transcript": self.path, "cli": "claude"}
            self.calls.append((text, key, queue))
            return 200, {"ok": True}
        self.mgr.tab_hook = hook

    def mirror(self):
        code, out = self.mgr.create({"tab": {"id": "t1", "title": "x"}})
        self.assertEqual(code, 200, out)
        return self.mgr.get(out["chat"]["id"])

    def texts(self, chat):
        return [(e["t"], e.get("text") or e.get("name")) for e in chat.events]

    def test_the_converter_keeps_what_was_said(self):
        with open(self.path) as f:
            raws = [json.loads(line) for line in f]
        got = [e for r in raws for e in tc.transcript_events(r)]
        self.assertEqual([(e["t"], e.get("text") or e.get("name")) for e in got],
                         [("user", "fix the bug"), ("text", "Looking now."),
                          ("tool", "Bash"), ("result", "a.txt"), ("user", "/clear"),
                          ("text", "Fixed.")])

    def test_it_is_named_after_the_tab_and_does_not_run_claude(self):
        chat = self.mirror()
        self.assertEqual(chat.meta["title"], "MOT")
        self.assertTrue(chat.mirrored)
        self.assertFalse(chat.running)

    def test_first_look_shows_the_conversation_then_only_what_is_new(self):
        chat = self.mirror()
        chat.mirror_poll()
        self.assertEqual(self.texts(chat)[0], ("user", "fix the bug"))
        n = len(chat.events)
        chat.mirror_poll()
        self.assertEqual(len(chat.events), n)
        with open(self.path, "a") as f:
            f.write(json.dumps({"type": "assistant", "message": {"content": [
                {"type": "text", "text": "More."}]}}) + "\n")
        chat.mirror_poll()
        self.assertEqual(self.texts(chat)[-1], ("text", "More."))

    def test_every_tab_status_but_idle_shows_in_the_chat(self):
        chat = self.mirror()
        for tab, want in (("working", "working"), ("blocked", "waiting"), ("ready", "ready"),
                          ("background", "background"), ("exited", "exited"),
                          ("idle", "idle"), ("unknown", "idle")):
            self.status = tab
            chat.mirror_poll()
            self.assertEqual(chat.state, want, tab)

    def test_lines_that_are_no_message_do_not_make_it_new(self):
        # Claude Code writes its own bookkeeping into the transcript; the
        # list's "13m" must stay the time of the last message
        chat = self.mirror()
        chat.mirror_poll()
        said = [e for e in chat.events if e["t"] in ("user", "text")][-1]["ts"]
        with open(self.path, "a") as f:
            f.write(json.dumps({"type": "atis-latch", "timestamp": "2099-01-01T00:00:00Z"}) + "\n")
        time.sleep(0.05)
        chat.mirror_poll()
        self.assertEqual(chat.summary()["updated"], said)

    def test_a_half_written_line_waits(self):
        chat = self.mirror()
        chat.mirror_poll()
        n = len(chat.events)
        line = json.dumps({"type": "assistant", "message": {"content": [
            {"type": "text", "text": "Late."}]}})
        with open(self.path, "a") as f:
            f.write(line[:20])
        chat.mirror_poll()
        self.assertEqual(len(chat.events), n)
        with open(self.path, "a") as f:
            f.write(line[20:] + "\n")
        chat.mirror_poll()
        self.assertEqual(self.texts(chat)[-1], ("text", "Late."))

    def test_a_new_transcript_is_followed(self):
        chat = self.mirror()
        chat.mirror_poll()
        self.path = os.path.join(self.dir, "tx2.jsonl")
        with open(self.path, "w") as f:
            f.write(json.dumps({"type": "user", "message": {"content": "again"}}) + "\n")
        chat.mirror_poll()
        kinds = self.texts(chat)[-2:]
        self.assertEqual(kinds, [("notice", "The tab started a new conversation."),
                                 ("user", "again")])

    def test_it_comes_back_after_a_restart_without_repeating(self):
        chat = self.mirror()
        chat.mirror_poll()
        n = len(chat.events)
        again = self.make()
        again.tab_hook = self.mgr.tab_hook
        self.addCleanup(again.stop_mirrors)
        back = again.get(chat.id)
        back.mirror_poll()
        self.assertEqual(len(back.events), n)
        self.assertEqual(back.seq, chat.seq)

    def test_a_reply_typed_in_a_chat_comes_back_as_a_reply_that_can_be_found(self):
        chat = self.mirror()
        chat.mirror_poll()
        said = next(e for e in chat.events if e["t"] == "text" and e["text"] == "Fixed.")
        lines = [{"type": "user", "message": {"content": '[In reply to Claude: "Fixed."]\nthanks'}},
                 {"type": "user", "message": {"content": '[In reply to Claude: "never said"]\nwho?'}}]
        with open(self.path, "a") as f:
            f.write("".join(json.dumps(x) + "\n" for x in lines))
        chat.mirror_poll()
        found, lost = [e for e in chat.events if e["t"] == "user"][-2:]
        self.assertEqual((found["text"], found["reply"]),
                         ("thanks", {"who": "Claude", "snip": "Fixed.", "seq": said["seq"]}))
        self.assertEqual((lost["text"], lost["reply"]["seq"]), ("who?", None))

    # -- a question the tab asks (AskUserQuestion)

    QUESTIONS = [
        {"question": "Pick a color", "header": "Color", "multiSelect": False,
         "options": [{"label": "Red"}, {"label": "Blue"}, {"label": "Green"}]},
        {"question": "Pick a size", "header": "Size", "multiSelect": False,
         "options": [{"label": "Small"}, {"label": "Large"}]}]

    def ask_in_tab(self, questions=None, answered=False):
        use = {"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": "tu9", "name": "AskUserQuestion",
             "input": {"questions": questions or self.QUESTIONS}}]}}
        lines = [use]
        if answered:
            lines.append({"type": "user", "message": {"content": [
                {"type": "tool_result", "tool_use_id": "tu9", "content": "done"}]}})
        with open(self.path, "a") as f:
            f.write("".join(json.dumps(x) + "\n" for x in lines))

    def asking(self, questions=None):
        self.addCleanup(setattr, tc, "ANSWER_STEP", tc.ANSWER_STEP)
        tc.ANSWER_STEP = 0
        chat = self.mirror()
        chat.mirror_poll()
        self.ask_in_tab(questions)
        self.status = "blocked"
        chat.mirror_poll()
        return chat

    def test_a_question_in_the_tab_becomes_a_card_with_its_options(self):
        chat = self.asking()
        (rid, held), = chat.pending.items()
        self.assertEqual(rid, "tab-tu9")
        self.assertEqual(chat.state, "waiting")
        ev = held["ev"]
        self.assertEqual((ev["t"], ev["tool"]), ("ask", "AskUserQuestion"))
        self.assertEqual([q["question"] for q in ev["questions"]], ["Pick a color", "Pick a size"])
        self.assertEqual([o["label"] for o in ev["questions"][0]["options"]], ["Red", "Blue", "Green"])
        n = len(chat.pending)
        chat.mirror_poll()                        # again: still one card
        self.assertEqual(len(chat.pending), n)
        self.assertEqual(len([e for e in chat.events if e["t"] == "ask"]), 1)

    def test_tapping_the_answers_presses_the_keys_that_answer_the_tab(self):
        chat = self.asking()
        code, out = tc.api(self.mgr, "POST", [chat.id, "answer"], {
            "rid": "tab-tu9", "allow": True,
            "answers": {"Pick a color": "Blue", "Pick a size": "Small"}})
        self.assertEqual(code, 200, out)
        self.assertEqual([k for _t, k, _q in self.calls], ["2", "1", "1"])    # Blue, Small, submit
        self.assertEqual(chat.pending, {})
        self.assertEqual(chat.events[-1]["t"], "answered")
        self.assertEqual(chat.events[-1]["answers"], {"Pick a color": "Blue", "Pick a size": "Small"})
        chat.mirror_poll()                        # the transcript is not caught up: no second card
        self.assertEqual(chat.pending, {})
        self.assertEqual(len([e for e in chat.events if e["t"] == "ask"]), 1)

    def test_one_question_needs_no_submit_and_skip_presses_escape(self):
        chat = self.asking(self.QUESTIONS[:1])
        tc.api(self.mgr, "POST", [chat.id, "answer"],
               {"rid": "tab-tu9", "allow": True, "answers": {"Pick a color": "Green"}})
        self.assertEqual([k for _t, k, _q in self.calls], ["3"])
        self.calls.clear()
        self.ask_in_tab(self.QUESTIONS[:1])       # a new question, a new tool id would be used
        chat2 = self.asking(self.QUESTIONS[:1])
        tc.api(self.mgr, "POST", [chat2.id, "answer"], {"rid": "tab-tu9", "allow": False})
        self.assertEqual([k for _t, k, _q in self.calls], ["esc"])

    def test_a_wrong_answer_is_refused_and_the_card_stays(self):
        chat = self.asking()
        code, out = tc.api(self.mgr, "POST", [chat.id, "answer"], {
            "rid": "tab-tu9", "allow": True, "answers": {"Pick a color": "Purple"}})
        self.assertEqual(code, 409)
        self.assertEqual(self.calls, [])
        self.assertIn("tab-tu9", chat.pending)

    def test_the_card_goes_when_the_tab_is_answered_there(self):
        chat = self.asking()
        self.ask_in_tab(answered=True)             # not the same tool id, so add the result for tu9
        with open(self.path, "a") as f:
            f.write(json.dumps({"type": "user", "message": {"content": [
                {"type": "tool_result", "tool_use_id": "tu9", "content": "x"}]}}) + "\n")
        self.status = "working"
        chat.mirror_poll()
        self.assertEqual(chat.pending, {})
        self.assertEqual(chat.events[-1]["t"], "answered")

    def test_a_question_answered_at_the_desk_is_not_answered_again(self):
        # the card is up, the tab was answered at the desk, the phone has not
        # looked again yet: a tap must not press keys into the next prompt
        chat = self.asking()
        with open(self.path, "a") as f:
            f.write(json.dumps({"type": "user", "message": {"content": [
                {"type": "tool_result", "tool_use_id": "tu9", "content": "x"}]}}) + "\n")
        self.status = "working"
        code, out = tc.api(self.mgr, "POST", [chat.id, "answer"], {
            "rid": "tab-tu9", "allow": False})
        self.assertEqual(code, 409)
        self.assertIn("no longer", out["error"])
        self.assertEqual(self.calls, [])
        self.assertEqual(chat.pending, {})

    def test_no_card_for_an_answered_or_multiple_choice_question(self):
        chat = self.mirror()
        chat.mirror_poll()
        self.ask_in_tab(answered=True)
        self.status = "blocked"
        chat.mirror_poll()
        self.assertEqual(chat.pending, {})
        many = [dict(self.QUESTIONS[0], multiSelect=True)]
        chat2 = self.asking(many)
        self.assertEqual(chat2.pending, {})

    def test_typing_waits_while_the_card_is_open(self):
        chat = self.asking()
        code, out = tc.api(self.mgr, "POST", [chat.id, "send"], {"text": "hello"})
        self.assertEqual(code, 409)
        self.assertIn("card", out["error"])
        self.assertEqual(self.calls, [])

    def test_state_follows_the_tab(self):
        chat = self.mirror()
        for tab, want in (("working", "working"), ("blocked", "waiting"), ("idle", "idle")):
            self.status = tab
            chat.mirror_poll()
            self.assertEqual(chat.state, want)

    def test_typing_goes_to_the_tab_with_no_bubble_of_its_own(self):
        chat = self.mirror()
        n = len(chat.events)
        code, _ = tc.api(self.mgr, "POST", [chat.id, "send"], {"text": "go on"})
        self.assertEqual(code, 200)
        self.assertEqual(self.calls, [("go on", None, True)])
        self.assertEqual(len(chat.events), n)
        self.status = "working"
        tc.api(self.mgr, "POST", [chat.id, "stop"], {})
        self.assertEqual(self.calls[-1], (None, "esc", False))

    def test_stop_presses_esc_only_while_the_tab_works(self):
        chat = self.mirror()
        self.assertFalse(chat.interrupt())                  # idle: Esc would open Rewind
        self.status = "working"
        self.assertTrue(chat.interrupt())
        self.assertFalse(chat.interrupt())                  # tapped again at once: one Esc
        self.assertEqual([k for _t, k, _q in self.calls], ["esc"])
        chat._esc_at -= tc.STOP_AGAIN
        old = self.mgr.tab_hook
        self.mgr.tab_hook = lambda tab, text=None, key=None, **kw: (503, {}) if key else old(tab, text, key, **kw)
        self.assertFalse(chat.interrupt())                  # tabit busy: the Esc did not go
        self.mgr.tab_hook = old
        self.status = "blocked"                             # a question: Esc says no
        self.assertTrue(chat.interrupt())
        self.assertEqual([k for _t, k, _q in self.calls], ["esc", "esc"])

    def test_only_a_claude_or_codex_tab(self):
        old = self.mgr.tab_hook
        self.mgr.tab_hook = lambda tab, *a, **k: dict(old(tab), cli="")
        self.assertEqual(self.mgr.create({"tab": {"id": "t1"}})[0], 400)
        self.mgr.tab_hook = old
        self.assertEqual(self.mgr.create({"tab": {"id": "zz"}})[0], 400)
        self.assertEqual(self.mgr.create({"tab": {"id": "a b;rm"}})[0], 400)

    def test_a_mirror_joins_a_group_and_hears_it_through_its_tab(self):
        chat = self.mirror()
        chat.mirror_poll()
        bob = self.new(title="Bob")
        code, out = self.mgr.create_group({"title": "Team", "members": [chat.id, bob.id]})
        self.assertEqual(code, 200, out)
        group = self.mgr.groups[out["group"]["id"]]
        group.send("@mot hello")
        text, key, queue = self.calls[-1]
        self.assertTrue(queue)
        self.assertIn("[user]: hello", text)                   # typed into the tab
        with open(self.path, "a") as f:                         # the tab answers, naming bob
            f.write(json.dumps({"type": "user", "message": {"content": text}}) + "\n")
            f.write(json.dumps({"type": "assistant", "message": {"content": [
                {"type": "text", "text": "@bob: can you check it"}]}}) + "\n")
        chat.mirror_poll()
        self.assertIn(("mot", "@bob: can you check it"),
                      [(e["name"], e["text"]) for e in group.events if e["t"] == "say"])
        self.assertTrue(wait(lambda: any(e["t"] == "relay" and e["to"] == "bob" for e in group.events)))
        self.assertTrue(wait(lambda: any("[mot]: @bob: can you check it" in e["text"]
                                         for e in bob.events if e["t"] == "user")))

    def test_what_the_tab_is_asked_at_the_desk_stays_out_of_the_group(self):
        chat = self.mirror()
        chat.mirror_poll()
        bob = self.new(title="Bob")
        code, out = self.mgr.create_group({"title": "Team", "members": [chat.id, bob.id]})
        group = self.mgr.groups[out["group"]["id"]]
        with open(self.path, "a") as f:                         # typed at the desk
            f.write(json.dumps({"type": "user", "message": {"content": "just for you"}}) + "\n")
            f.write(json.dumps({"type": "assistant", "message": {"content": [
                {"type": "text", "text": "@bob: a private answer"}]}}) + "\n")
        chat.mirror_poll()
        time.sleep(0.3)
        self.assertEqual([e for e in group.events if e["t"] in ("say", "relay")], [])
        self.assertFalse(any(e["t"] == "user" for e in bob.events))

    def test_notes_and_tool_results_in_a_group_turn_keep_it_the_groups(self):
        chat = self.mirror()
        chat.mirror_poll()
        bob = self.new(title="Bob")
        code, out = self.mgr.create_group({"title": "Team", "members": [chat.id, bob.id]})
        group = self.mgr.groups[out["group"]["id"]]
        group.send("@mot look")
        with open(self.path, "a") as f:
            for raw in ({"type": "user", "message": {"content": self.calls[-1][0]}},
                        {"type": "user", "message": {"content": "<system-reminder>x</system-reminder>"}},
                        {"type": "user", "message": {"content": [
                            {"type": "tool_result", "tool_use_id": "t1", "content": "ok"}]}},
                        {"type": "assistant", "message": {"content": [
                            {"type": "text", "text": "seen it"}]}}):
                f.write(json.dumps(raw) + "\n")
        chat.mirror_poll()
        self.assertIn(("mot", "seen it"),
                      [(e["name"], e["text"]) for e in group.events if e["t"] == "say"])

    def group_turn(self, *lines):
        """A mirror in a group, answering the group's question; the lines go
        into its transcript after that question."""
        chat = self.mirror()
        chat.mirror_poll()
        bob = self.new(title="Bob")
        code, out = self.mgr.create_group({"title": "Team", "members": [chat.id, bob.id]})
        group = self.mgr.groups[out["group"]["id"]]
        group.send("@mot look")
        with open(self.path, "a") as f:
            for raw in ({"type": "user", "message": {"content": self.calls[-1][0]}},) + lines:
                f.write(json.dumps(raw) + "\n")
        chat.mirror_poll()
        return chat, group

    @staticmethod
    def aside(text):
        return {"type": "attachment", "attachment": {"type": "queued_command", "commandMode": "prompt",
                                                     "origin": {"kind": "human"}, "prompt": text}}

    @staticmethod
    def answer(text):
        return {"type": "assistant", "message": {"content": [{"type": "text", "text": text}]}}

    def says_of(self, group):
        return [(e["name"], e["text"]) for e in group.events if e["t"] == "say"]

    def test_an_aside_typed_at_the_desk_mid_turn_leaves_the_group_its_answer(self):
        chat, group = self.group_turn(self.aside("also tidy up"), self.answer("seen it"))
        self.assertIn(("mot", "seen it"), self.says_of(group))

    def test_a_line_sent_on_to_tabs_leaves_the_group_its_answer(self):
        chat, group = self.group_turn()
        chat.emit({"t": "user", "text": "@beta look", "to": ["beta"]})   # what send_to_tabs shows
        with open(self.path, "a") as f:
            f.write(json.dumps(self.answer("seen it")) + "\n")
        chat.mirror_poll()
        self.assertIn(("mot", "seen it"), self.says_of(group))

    def test_a_group_line_read_mid_turn_makes_the_rest_the_groups(self):
        chat = self.mirror()
        chat.mirror_poll()
        bob = self.new(title="Bob")
        code, out = self.mgr.create_group({"title": "Team", "members": [chat.id, bob.id]})
        group = self.mgr.groups[out["group"]["id"]]
        with open(self.path, "a") as f:
            for raw in ({"type": "user", "message": {"content": "mine only"}}, self.answer("private part"),
                        self.aside("[user]: and the group?"), self.answer("for all")):
                f.write(json.dumps(raw) + "\n")
        chat.mirror_poll()
        self.assertEqual(self.says_of(group), [("mot", "for all")])

    def test_old_lines_of_a_mirror_are_not_passed_on(self):
        # the first look reads the tab's past: it is history, not news
        with open(self.path, "a") as f:
            f.write(json.dumps({"type": "assistant", "message": {"content": [
                {"type": "text", "text": "@bob: an old line"}]}}) + "\n")
        chat, bob = self.mirror(), self.new(title="Bob")
        code, out = self.mgr.create_group({"members": [chat.id, bob.id]})
        group = self.mgr.groups[out["group"]["id"]]
        chat.mirror_poll()
        self.assertEqual([e for e in group.events if e["t"] in ("say", "relay")], [])
        self.assertFalse(any(e["t"] == "user" for e in bob.events))



class CodexMirrorTest(Base):
    """A chat that shows a Codex tab: its bubbles come from the tab's rollout file."""

    def setUp(self):
        super().setUp()
        self.addCleanup(setattr, tc, "MIRROR_EVERY", tc.MIRROR_EVERY)
        tc.MIRROR_EVERY = 3600       # the tests look by hand
        self.path = os.path.join(self.dir, "rollout.jsonl")
        shutil.copy(os.path.join(os.path.dirname(__file__), "fixtures", "chat",
                                 "codex_rollout.jsonl"), self.path)
        self.mgr.tab_hook = lambda tab, text=None, key=None, queue=False, screen=False: {
            "title": "MOT", "status": "idle", "transcript": self.path, "cli": "codex"}
        self.mgr.tabs_hook = lambda: [{"id": "t1", "title": "MOT", "cli": "codex"}]

    mirror = MirrorTest.mirror
    texts = MirrorTest.texts

    def raws(self):
        with open(self.path) as f:
            return [json.loads(line) for line in f]

    def test_the_converter_keeps_what_was_said(self):
        got = [e for r in self.raws() for e in tc.transcript_events(r, "codex")]
        # the context Codex adds and its response_item copies are not shown
        self.assertEqual([(e["t"], e.get("text") or e.get("name")) for e in got],
                         [("user", "fix the bug"), ("text", "Looking now."),
                          ("tool", "Bash"), ("result", "a.txt\n"),
                          ("tool", "Bash"), ("result", "cat: b.txt: No such file\n"),
                          ("tool", "Edit"), ("text", "Fixed.")])
        tools = [e for e in got if e["t"] in ("tool", "result")]
        self.assertEqual(tools[0]["summary"], "ls")
        self.assertEqual([e["error"] for e in tools if e["t"] == "result"], [False, True])
        self.assertEqual(tools[4]["summary"], "/home/u/proj/a.txt")
        self.assertTrue(all("ts" in e for e in got))

    def test_an_interrupt_is_a_note(self):
        raw = {"type": "event_msg", "payload": {"type": "turn_aborted", "reason": "interrupted"}}
        self.assertEqual(tc.codex_events(raw), [{"t": "notice", "text": "Interrupted."}])

    def test_the_model_comes_from_the_turn(self):
        self.assertEqual(tc.transcript_model(self.path), "gpt-5.6-luna")

    def test_first_look_shows_the_conversation_then_only_what_is_new(self):
        chat = self.mirror()
        chat.mirror_poll()
        self.assertEqual(self.texts(chat)[0], ("user", "fix the bug"))
        self.assertEqual(chat.meta["tab"].get("model"), "gpt-5.6-luna")
        n = len(chat.events)
        chat.mirror_poll()
        self.assertEqual(len(chat.events), n)

    def test_a_codex_tab_gets_a_chat(self):
        self.mgr.sync_tabs()
        chat = next(c for c in self.mgr.chats.values() if c.mirrored)
        self.assertEqual(chat.meta["tab"]["cli"], "codex")

    def test_its_usage_comes_from_the_rollout(self):
        line = {"timestamp": "2026-11-05T09:31:11.000Z", "type": "event_msg",
                "payload": {"type": "token_count", "rate_limits": {
                    "primary": {"used_percent": 12.4, "window_minutes": 300, "resets_at": 1900000000},
                    "secondary": {"used_percent": 40, "window_minutes": 10080, "resets_at": 1900500000}}}}
        self.assertEqual(tc.codex_limits(line), [{"name": "5h", "pct": 12, "resets": 1900000000},
                                                 {"name": "7d", "pct": 40, "resets": 1900500000}])
        free = {"type": "event_msg", "payload": {"type": "token_count", "rate_limits": {
            "primary": {"used_percent": 6, "window_minutes": 43200}, "secondary": None}}}
        self.assertEqual([w["name"] for w in tc.codex_limits(free)], ["30d"])   # a free plan: one window
        with open(self.path, "a") as f:
            f.write(json.dumps(line) + "\n")
        chat = self.mirror()
        chat.mirror_poll()
        self.assertEqual(self.mgr.usage(), [{"cli": "codex", "account": "",
                                             "windows": tc.codex_limits(line), "ts": tc._raw_time(line)}])
        # kept over a restart: the mirror reads only new lines then
        again = self.make()
        self.addCleanup(again.stop_mirrors)
        self.assertEqual(again.usage()[0]["windows"], tc.codex_limits(line))

    def codex_home(self):
        home = os.path.join(self.dir, "codex-home")
        os.makedirs(home)
        with open(os.path.join(home, "models_cache.json"), "w") as f:
            lv = lambda *e: [{"effort": x} for x in e]
            json.dump({"models": [{"slug": "gpt-a", "display_name": "GPT-A", "visibility": "list",
                                   "supported_reasoning_levels": lv("low", "high", "ultra")},
                                  {"slug": "gpt-b", "display_name": "GPT-B", "visibility": "list",
                                   "supported_reasoning_levels": lv("low", "high")},
                                  {"slug": "gpt-hidden", "visibility": "hide"}]}, f)
        p = mock.patch.dict(os.environ, {"CODEX_HOME": home})
        p.start()
        self.addCleanup(p.stop)

    def test_the_models_are_those_codex_lists(self):
        self.codex_home()
        self.assertEqual(tc.codex_models(), [
            {"name": "", "label": "default", "efforts": ["", "low", "high"]},   # what all take
            {"name": "gpt-a", "label": "GPT-A", "efforts": ["", "low", "high", "ultra"]},
            {"name": "gpt-b", "label": "GPT-B", "efforts": ["", "low", "high"]}])

    def test_the_effort_is_one_the_model_takes(self):
        self.codex_home()
        chat = self.mirror()
        chat.mirror_poll()
        ops = []
        self.mgr.tab_ops = lambda op, target=None, body=None: ops.append((op, body)) or (200, {"ok": True})
        edit = lambda body: tc.api(self.mgr, "POST", [chat.id, "edit"], body)[0]
        self.assertEqual(edit({"model": "gpt-b", "effort": "ultra"}), 400)    # gpt-b has no ultra
        self.assertEqual(edit({"effort": "nope"}), 400)
        self.assertEqual(ops, [])
        self.assertEqual(edit({"model": "gpt-a", "effort": "ultra"}), 200)
        self.assertEqual(edit({"effort": "low"}), 200)                         # the effort alone
        self.assertEqual(ops, [("model", {"model": "gpt-a", "effort": "ultra"}),
                               ("model", {"effort": "low"})])
        self.assertEqual(chat.meta["tab"]["effort"], "low")

    def test_its_login_and_model_switch_as_codex(self):
        self.codex_home()
        chat = self.mirror()
        chat.mirror_poll()                              # it answers as gpt-5.6-luna
        ops = []
        self.mgr.tab_ops = lambda op, target=None, body=None: ops.append((op, body)) or (200, {"ok": True})
        self.mgr.accounts = lambda: [("Work", FAKE)]
        self.mgr.codex_accounts = lambda: [("Home", FAKE)]
        edit = lambda body: tc.api(self.mgr, "POST", [chat.id, "edit"], body)[0]
        self.assertEqual(edit({"model": "opus"}), 400)          # a Claude model
        self.assertEqual(edit({"account": "Work"}), 400)        # a Claude login
        self.assertEqual(edit({"model": "gpt-5.6-luna"}), 200)  # the one it has: nothing to do
        self.assertEqual(ops, [])
        self.assertEqual(edit({"model": "gpt-a", "account": "Home"}), 200)
        self.assertEqual(ops, [("model", {"model": "gpt-a", "account": "Home"})])
        self.assertEqual((chat.meta["tab"]["model"], chat.summary()["account"]), ("gpt-a", "Home"))

    def test_the_page_gets_the_codex_logins_and_models(self):
        self.codex_home()
        self.mgr.codex_accounts = lambda: [("Home", FAKE)]
        with mock.patch.object(tc.shutil, "which", lambda c: "/usr/bin/" + c):
            out = tc.api(self.mgr, "GET", [], None)[1]
        self.assertEqual([a["name"] for a in out["codex_accounts"]], ["", "Home"])
        self.assertEqual([m["name"] for m in out["codex_models"]], ["", "gpt-a", "gpt-b"])
        self.assertEqual(out["efforts"], list(tc.EFFORTS))

    def test_another_ai_tab_gets_none(self):
        self.mgr.tab_hook = lambda tab, text=None, key=None, queue=False, screen=False: {
            "title": "G", "status": "idle", "transcript": "", "cli": ""}
        code, out = self.mgr.create({"tab": {"id": "t1", "title": "x"}})
        self.assertEqual(code, 400, out)


class SessionListTest(unittest.TestCase):
    """The past conversations of a folder, for the new chat form."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = self.tmp.name

    def write(self, path, lines, mtime):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write("".join(json.dumps(x) + "\n" for x in lines))
        os.utime(path, (mtime, mtime))

    def codex(self, sid, cwd, said, mtime):
        typed = [{"type": "event_msg", "payload": {"type": "item_completed", "item": {
            "type": "UserMessage", "id": "u1", "content": [{"type": "text", "text": said}]}}}] if said else []
        self.write(os.path.join(self.root, "2026", "11", "05", "rollout-2026-11-05T17-30-59-%s.jsonl" % sid),
                   [{"type": "session_meta", "payload": {"id": sid, "cwd": cwd}},
                    {"type": "response_item", "payload": {"type": "message", "role": "user",
                                                          "content": [{"type": "input_text", "text": "# AGENTS.md"}]}}]
                   + typed, mtime)

    def test_codex_lists_the_folders_conversations_newest_first(self):
        self.codex("a" * 36, "/w", "first job", 100)
        self.codex("b" * 36, "/w", "second   job", 200)
        self.codex("c" * 36, "/other", "elsewhere", 300)
        self.codex("d" * 36, "/w", "", 400)               # nobody typed in it: nothing to go on with
        got = tc.codex_sessions("/w", root=self.root)
        self.assertEqual([(s["id"][0], s["title"], s["ts"]) for s in got],
                         [("b", "second job", 200), ("a", "first job", 100)])

    def test_codex_reads_every_rollout_not_only_the_newest(self):
        for i in range(400):                     # another project's, newer
            self.codex("%036d" % i, "/other", "elsewhere", 1000 + i)
        self.codex("e" * 36, "/w", "old one here", 10)
        self.assertEqual([s["title"] for s in tc.codex_sessions("/w", root=self.root)], ["old one here"])

    def test_a_long_session_meta_is_still_read(self):
        # the folder and id come first; the instructions after them run long
        path = os.path.join(self.root, "2026", "11", "05", "rollout-x-%s.jsonl" % ("f" * 36))
        self.write(path, [{"type": "session_meta", "payload": {"id": "f" * 36, "cwd": "/w",
                                                               "base_instructions": {"text": "x" * 50000}}},
                          {"type": "event_msg", "payload": {"type": "item_completed", "item": {
                              "type": "UserMessage", "id": "u", "content": [{"type": "text", "text": "hi"}]}}}], 5)
        self.assertEqual([s["id"] for s in tc.codex_sessions("/w", root=self.root)], ["f" * 36])

    def test_grok_lists_one_conversation_once(self):
        real = os.path.join(self.root, "real")
        os.makedirs(real)
        link = os.path.join(self.root, "link")
        os.symlink(real, link)
        for folder in (link, real):
            self.write(os.path.join(self.root, "g", urllib.parse.quote(folder, safe=""), "1" * 36, "updates.jsonl"),
                       [{"method": "session/update", "params": {"update": {
                           "sessionUpdate": "user_message_chunk", "content": {"type": "text", "text": "hi"}}}}], 5)
        self.assertEqual(len(tc.grok_sessions(link, root=os.path.join(self.root, "g"))), 1)

    def test_grok_lists_the_folders_conversations(self):
        def grok(sid, said, mtime, hidden=False):
            self.write(os.path.join(self.root, urllib.parse.quote("/w", safe=""), sid, "updates.jsonl"),
                       [{"method": "session/update", "params": {"update": {
                           "sessionUpdate": "user_message_chunk", "content": {"type": "text", "text": said},
                           "_meta": {"hideFromScrollback": hidden}}}}], mtime)
        grok("1" * 36, "hello grok", 100)
        grok("2" * 36, "a note Grok wrote itself", 200, hidden=True)
        self.assertEqual([(s["id"], s["title"]) for s in tc.grok_sessions("/w", root=self.root)],
                         [("1" * 36, "hello grok")])
        self.assertEqual(tc.grok_sessions("/nowhere", root=self.root), [])


class GrokMirrorTest(Base):
    """A chat that shows a Grok tab: its bubbles come from the session's updates.jsonl."""

    def setUp(self):
        super().setUp()
        self.addCleanup(setattr, tc, "MIRROR_EVERY", tc.MIRROR_EVERY)
        tc.MIRROR_EVERY = 3600       # the tests look by hand
        self.path = os.path.join(self.dir, "updates.jsonl")
        shutil.copy(os.path.join(os.path.dirname(__file__), "fixtures", "chat",
                                 "grok_updates.jsonl"), self.path)
        self.status, self.calls, self.box = "idle", [], "❯"

        def hook(tab, text=None, key=None, queue=False, screen=False):
            if text is None and key is None:
                return dict({"title": "Fox", "status": self.status, "transcript": self.path, "cli": "grok"},
                            **({"screen": ["Worked for 3s", "minimal · /help", self.box,
                                           "Grok 4.7 (high) · always-approve"]} if screen else {}))
            self.calls.append((text, key, queue))
            return 200, {"ok": True}
        self.mgr.tab_hook = hook
        self.mgr.tabs_hook = lambda: [{"id": "t1", "title": "Fox", "cli": "grok"}]

    mirror = MirrorTest.mirror
    texts = MirrorTest.texts

    def test_the_converter_keeps_what_was_said(self):
        with open(self.path) as f:
            got = [e for line in f for e in tc.transcript_events(json.loads(line), "grok")]
        # its thinking, its own notes and an image are not shown
        self.assertEqual([(e["t"], e.get("text") or e.get("name")) for e in got],
                         [("user", "fix the bug"), ("text", "Looking now."),
                          ("tool", "Bash"), ("result", "a.txt"),
                          ("tool", "Read"), ("result", "No such file: /home/u/proj/b.txt"),
                          ("tool", "Edit"), ("result", got[7]["text"]),
                          ("tool", "LS"), ("result", "- /home/u/proj/\n  - a.txt"),
                          ("text", "Fixed."), ("user", "and this one"),
                          ("notice", "Interrupted.")])
        tools = [e for e in got if e["t"] in ("tool", "result")]
        self.assertEqual([e["summary"] for e in tools if e["t"] == "tool"],
                         ["ls", "/home/u/proj/b.txt", "/home/u/proj/a.txt", "/home/u/proj"])
        self.assertEqual([e["error"] for e in tools if e["t"] == "result"], [False, True, False, False])
        self.assertIn("-x = 1\n+x = 2", got[7]["text"])            # an edit shows its diff
        self.assertEqual(got[0]["ts"], 1793000001)                  # Grok's time: epoch seconds

    def test_a_result_kept_only_in_raw_output_is_shown(self):
        def result(raw, status="completed"):
            line = {"method": "session/update", "params": {"update": {
                "sessionUpdate": "tool_call_update", "toolCallId": "c", "status": status, "rawOutput": raw}}}
            return tc.grok_events(line)[0]["text"]
        self.assertEqual(result({"type": "ListDir", "NotFound": "Error: /x does not exist."}, "failed"),
                         "Error: /x does not exist.")
        self.assertEqual(result({"type": "KillTask", "Result": {"outcome": "killed",
                                                               "message": "Task was terminated"}}),
                         "Task was terminated")
        self.assertEqual(result({"type": "Bash", "output_for_prompt": "boom"}, "failed"), "boom")
        self.assertEqual(result({"type": "Bash", "output_for_prompt": "", "exit_code": 2}, "failed"),
                         "exit code 2")
        self.assertEqual(result({"action": {"type": "search", "query": "tabit grok",
                                            "sources": [{"type": "url", "url": "https://a.example/"}]}}),
                         "tabit grok\nhttps://a.example/")

    def test_an_error_ends_the_turn_with_why(self):
        raw = {"method": "_x.ai/session/update", "params": {"update": {
            "sessionUpdate": "turn_completed", "stop_reason": "error",
            "agent_result": "API error (status 402): balance exhausted"}}}
        self.assertEqual(tc.grok_events(raw), [
            {"t": "error", "text": "Grok stopped: API error (status 402): balance exhausted"}])

    def test_the_model_comes_from_the_last_prompt(self):
        self.assertEqual(tc.transcript_model(self.path), "grok-4.8")

    def test_a_grok_tab_gets_a_chat_that_shows_it(self):
        self.mgr.sync_tabs()
        chat = next(c for c in self.mgr.chats.values() if c.mirrored)
        self.assertEqual(chat.meta["tab"]["cli"], "grok")
        chat.mirror_poll()
        self.assertEqual(self.texts(chat)[:2], [("user", "fix the bug"), ("text", "Looking now.")])
        self.assertEqual(chat.meta["tab"].get("model"), "grok-4.8")
        self.assertEqual(self.mgr.listing()[0]["tab"]["cli"], "grok")

    def test_a_message_is_typed_into_the_tab(self):
        chat = self.mirror()
        self.assertEqual(tc.api(self.mgr, "POST", [chat.id, "send"], {"text": "go on"})[0], 200)
        self.assertEqual(self.calls, [("go on", None, True)])

    def test_stop_presses_ctrl_c_once_while_grok_works(self):
        # Esc never stops a Grok turn
        self.addCleanup(setattr, tc, "GROK_STOP_LOOK", tc.GROK_STOP_LOOK)
        tc.GROK_STOP_LOOK = 0.05
        chat = self.mirror()
        self.assertFalse(chat.interrupt())                   # idle: nothing to stop
        self.status = "blocked"
        self.assertFalse(chat.interrupt())                   # it asks: Ctrl+C would only open a panel
        self.status, self.box = "working", "❯ half a line typed at the desk"
        self.assertFalse(chat.interrupt())                   # Ctrl+C would only clear the draft
        self.box = "❯"
        self.assertTrue(chat.interrupt())
        self.assertFalse(chat.interrupt())                   # again soon: it heads for quitting Grok
        self.assertEqual([k for _t, k, _q in self.calls], ["ctrl-c"])
        # still working a moment later (a panel about its sub-agents): the chat says so
        self.assertTrue(wait(lambda: any(e["t"] == "notice" and "not stopped" in e["text"]
                                         for e in chat.events)))
        chat._esc_at -= tc.GROK_STOP_AGAIN
        n = len(chat.events)
        self.assertTrue(chat.interrupt())
        self.status = "idle"                                 # this time it stopped
        time.sleep(0.2)
        self.assertEqual(len(chat.events), n)

    def test_no_login_model_commands_or_usage_of_its_own(self):
        chat = self.mirror()
        chat.mirror_poll()
        ops = []
        self.mgr.tab_ops = lambda op, target=None, body=None: ops.append(op) or (200, {"ok": True})
        edit = lambda body: tc.api(self.mgr, "POST", [chat.id, "edit"], body)[0]
        self.assertEqual(edit({"model": "opus"}), 400)
        self.assertEqual(edit({"effort": "high"}), 400)
        self.assertEqual(edit({"account": "Work"}), 400)
        self.assertEqual(edit({"title": "Fox 2"}), 200)              # its name still changes
        self.assertEqual(ops, ["rename"])
        self.assertEqual(chat.commands(), {"cli": "grok", "builtins": [], "skills": []})

    def test_its_usage_is_asked_of_grok(self):
        chat = self.mirror()
        chat.mirror_poll()
        self.assertEqual(self.mgr.usage(), [{"cli": "grok", "account": "", "windows": [], "ts": 0}])
        week = [{"name": "7d", "pct": 16, "resets": 1794744301.0}]
        asked = []
        self.mgr.grok_usage = lambda: asked.append(1) or week
        self.mgr.usage()
        self.assertTrue(wait(lambda: self.mgr.usage()[0]["windows"] == week))
        saved = os.path.join(self.mgr.root, "usage.json")
        self.assertTrue(wait(lambda: os.path.exists(saved) and not os.path.exists(saved + ".tmp")))
        self.mgr.usage()
        self.assertEqual(len(asked), 1)                  # not again for GROK_ASK seconds

    def test_billing_is_read_as_a_window(self):
        billing = {"config": {"creditUsagePercent": 16.4, "currentPeriod": {
            "type": "USAGE_PERIOD_TYPE_WEEKLY", "start": "2026-11-08T12:05:01.043+00:00",
            "end": "2026-11-15T12:05:01.043+00:00"}}, "subscription_tier": "SuperGrok"}
        self.assertEqual(tc.grok_limits(billing), [{"name": "7d", "pct": 16, "resets": 1794744301.0}])
        self.assertEqual(tc.grok_limits({"config": {}}), [])
        self.assertEqual(tc.grok_limits(None), [])

    def test_a_grok_with_no_screen_is_asked(self):
        fake = os.path.join(self.dir, "grok")
        with open(fake, "w") as f:
            f.write("#!/usr/bin/env python3\n"
                    "import json, sys\n"
                    "assert sys.argv[1:] == ['agent', '--no-leader', 'stdio'], sys.argv\n"
                    "for line in sys.stdin:\n"
                    "    m = json.loads(line)\n"
                    "    if m['method'] == 'initialize':\n"
                    "        print(json.dumps({'jsonrpc': '2.0', 'method': '_x.ai/models/update', 'params': {}}))\n"
                    "        print(json.dumps({'jsonrpc': '2.0', 'id': m['id'], 'result': {}}), flush=True)\n"
                    "    else:\n"
                    "        print(json.dumps({'jsonrpc': '2.0', 'id': m['id'], 'result': {'config': {\n"
                    "            'creditUsagePercent': 40, 'currentPeriod': {'type': 'USAGE_PERIOD_TYPE_MONTHLY',\n"
                    "            'end': '2026-12-01T00:00:00+00:00'}}}}), flush=True)\n")
        os.chmod(fake, 0o755)
        self.assertEqual(tc.grok_usage(fake), [{"name": "30d", "pct": 40, "resets": 1796083200.0}])
        self.assertIsNone(tc.grok_usage(os.path.join(self.dir, "none")))

    def test_a_grok_that_never_answers_goes_with_its_helpers(self):
        mark = "tabit-grok-helper-%d" % os.getpid()
        fake = os.path.join(self.dir, "grok")
        with open(fake, "w") as f:
            f.write("#!/bin/bash\nexec -a %s sleep 300 &\nsleep 300\n" % mark)
        os.chmod(fake, 0o755)
        start = time.time()
        self.assertIsNone(tc.grok_usage(fake, wait=0.5))
        self.assertLess(time.time() - start, 5)
        self.assertTrue(wait(lambda: subprocess.run(["pgrep", "-f", mark], capture_output=True).returncode != 0))

    def test_a_reply_line_calls_another_tab(self):
        other = os.path.join(self.dir, "beta.jsonl")
        shutil.copy(os.path.join(HERE, "fixtures", "chat", "tab_transcript.jsonl"), other)
        old = self.mgr.tab_hook

        def hook(tab, text=None, key=None, queue=False, screen=False):
            if tab == "t2" and text is None and key is None:
                return {"title": "Beta", "status": "idle", "transcript": other, "cli": "claude"}
            return old(tab, text, key, queue, screen)
        self.mgr.tab_hook = hook
        self.mgr.tabs_hook = lambda: [{"id": "t1", "title": "Fox", "cli": "grok"},
                                      {"id": "t2", "title": "Beta", "cli": "claude"}]
        self.mgr.sync_tabs()
        fox = next(c for c in self.mgr.chats.values() if c.meta["tab"]["id"] == "t1")
        fox.mirror_poll()
        with open(self.path, "a") as f:
            f.write(json.dumps({"timestamp": 1793000100, "method": "session/update", "params": {
                "update": {"sessionUpdate": "agent_message_chunk",
                           "content": {"type": "text", "text": "@beta: can you look?"}}}}) + "\n")
        fox.mirror_poll()
        self.assertIn(("[from @fox]: @beta: can you look?", None, True), self.calls)


class TabSyncTest(Base):
    """A chat and a Claude tab are one: same name, same group, made and closed together."""

    def setUp(self):
        super().setUp()
        self.addCleanup(setattr, tc, "MIRROR_EVERY", tc.MIRROR_EVERY)
        tc.MIRROR_EVERY = 3600       # the tests sync by hand
        self.tabs = [{"id": "t1", "title": "MOT", "group": "Work", "color": "blue"},
                     {"id": "t2", "title": "Blog", "group": "", "color": ""}]
        self.ops = []
        self.reply = (200, {"ok": True})
        self.mgr.tab_hook = lambda tab, text=None, key=None, queue=False, screen=False: {
            "title": "MOT", "status": "idle", "transcript": "", "cli": "claude"}
        self.mgr.tabs_hook = lambda: [dict(t) for t in self.tabs] if self.tabs is not None else None

        def ops(op, target=None, body=None):
            self.ops.append((op, target, body))
            if op == "new":
                return (200, {"id": "t9"}) if self.reply[0] == 200 else self.reply
            if op == "groups":
                return 200, {"groups": [{"color": "blue", "name": "Work"}]}
            return self.reply
        self.mgr.tab_ops = ops

    def titles(self):
        return sorted(c.meta["title"] for c in self.mgr.chats.values() if c.mirrored)

    def chat_of(self, tab):
        return next(c for c in self.mgr.chats.values() if c.mirrored and c.meta["tab"]["id"] == tab)

    def test_every_tab_gets_a_chat_once_with_its_name_and_group(self):
        self.mgr.sync_tabs()
        self.mgr.sync_tabs()
        self.assertEqual(self.titles(), ["Blog", "MOT"])
        tab = self.chat_of("t1").meta["tab"]
        self.assertEqual((tab["group"], tab["color"]), ("Work", "blue"))

    def test_a_chat_made_by_hand_takes_the_tab_name_too(self):
        self.mgr.create({"title": "Mine", "tab": {"id": "t1", "title": "MOT"}})
        self.mgr.sync_tabs()
        self.assertEqual(self.titles(), ["Blog", "MOT"])

    def test_the_name_and_group_follow_the_tab(self):
        self.mgr.sync_tabs()
        self.tabs[0].update(title="MOT 2", group="Cars", color="red")
        self.mgr.sync_tabs()
        chat = self.chat_of("t1")
        self.assertEqual(chat.meta["title"], "MOT 2")
        self.assertEqual(chat.meta["tab"]["group"], "Cars")

    def test_renaming_the_chat_renames_the_tab(self):
        self.mgr.sync_tabs()
        chat = self.chat_of("t1")
        self.assertEqual(tc.api(self.mgr, "POST", [chat.id, "edit"], {"title": "Car"})[0], 200)
        self.assertIn(("rename", "t1", {"name": "Car"}), self.ops)
        self.assertEqual(chat.meta["title"], "Car")

    def test_a_rename_the_tab_refuses_changes_nothing(self):
        self.mgr.sync_tabs()
        chat = self.chat_of("t1")
        self.reply = (400, {"error": "name of 1 to 80 characters"})
        self.assertEqual(tc.api(self.mgr, "POST", [chat.id, "edit"], {"title": "Car"})[0], 400)
        self.assertEqual(chat.meta["title"], "MOT")

    def test_saving_with_the_same_name_does_not_rename_the_tab(self):
        self.mgr.sync_tabs()
        chat = self.chat_of("t1")
        tc.api(self.mgr, "POST", [chat.id, "edit"], {"title": "MOT"})
        self.assertFalse([o for o in self.ops if o[0] == "rename"])

    def test_deleting_the_chat_closes_the_tab(self):
        self.mgr.sync_tabs()
        chat = self.chat_of("t2")
        self.assertEqual(tc.api(self.mgr, "POST", [chat.id, "delete"], {})[0], 200)
        self.assertIn(("close", "t2", {}), self.ops)
        self.assertEqual(self.titles(), ["MOT"])

    def test_the_chat_stays_when_the_tab_will_not_close(self):
        self.mgr.sync_tabs()
        chat = self.chat_of("t2")
        self.reply = (409, {"error": "that is the last tab"})
        code, out = tc.api(self.mgr, "POST", [chat.id, "delete"], {})
        self.assertEqual(code, 409)
        self.assertEqual(self.titles(), ["Blog", "MOT"])

    def test_the_chat_goes_when_the_tab_was_gone_already(self):
        self.mgr.sync_tabs()
        chat = self.chat_of("t2")
        self.reply = (404, {"error": "that tab is gone"})
        self.assertEqual(tc.api(self.mgr, "POST", [chat.id, "delete"], {})[0], 200)
        self.assertEqual(self.titles(), ["MOT"])

    def test_a_closed_tab_takes_its_chat_after_a_while(self):
        self.mgr.sync_tabs()
        del self.tabs[1]
        for _ in range(tc.GONE_AFTER - 1):
            self.mgr.sync_tabs()
        self.assertEqual(self.titles(), ["Blog", "MOT"])
        self.mgr.sync_tabs()
        self.assertEqual(self.titles(), ["MOT"])
        self.assertFalse([o for o in self.ops if o[0] == "close"])     # it is gone: nothing to close

    def test_no_answer_changes_nothing(self):
        self.mgr.sync_tabs()
        self.tabs = None
        for _ in range(tc.GONE_AFTER + 1):
            self.mgr.sync_tabs()
        self.assertEqual(self.titles(), ["Blog", "MOT"])

    def test_the_last_tab_closed_takes_its_chat_too(self):
        self.mgr.sync_tabs()
        self.tabs = []                   # no Claude tab at all: an answer, not tabit busy
        for _ in range(tc.GONE_AFTER):
            self.mgr.sync_tabs()
        self.assertEqual(self.titles(), [])

    def test_a_tab_with_no_group_stays_out_of_groups(self):
        self.mgr.sync_tabs()
        self.mgr.sync_tabs()
        self.assertEqual([o for o in self.ops if o[0] not in ("groups",)], [])
        tab = self.chat_of("t2").meta["tab"]
        self.assertEqual((tab["group"], tab["color"]), ("", ""))

    def news(self):
        return [o[2] for o in self.ops if o[0] == "new"]

    def old_chat(self, **kw):
        chat = self.new(**kw)
        chat.send("hi")
        wait(lambda: "done" in self.kinds(chat))
        return chat

    def test_an_old_chat_moves_into_a_tab_that_goes_on_with_its_conversation(self):
        chat = self.old_chat(mode="bypass", emoji="🏃", hue=40, title="Run")
        session, old_id = chat.meta["session"], chat.id
        self.mgr.sync_tabs()
        body = self.news()[0]
        self.assertEqual((body["resume"], body["name"], body["cwd"], body["bypass"], body["group"]),
                         (session, "Run", chat.meta["cwd"], True, ""))
        self.assertNotIn(old_id, self.mgr.chats)
        moved = self.chat_of("t9")
        self.assertEqual((moved.meta["emoji"], moved.meta["hue"], moved.meta["title"]), ("🏃", 40, "Run"))
        self.assertFalse(chat.running)
        # the old chat's folder is kept, not deleted: its history stays on disk
        kept = os.path.join(self.mgr.root, old_id + ".moved")
        self.assertTrue(os.path.isfile(os.path.join(kept, "events.jsonl")))
        self.assertFalse(os.path.exists(os.path.join(self.mgr.root, old_id)))
        self.assertNotIn(old_id, self.make().chats)          # and it does not load as a chat again

    def test_a_chat_that_never_spoke_moves_as_a_new_tab(self):
        self.new(title="Fresh")
        self.mgr.sync_tabs()
        self.assertNotIn("resume", self.news()[0])

    def test_the_main_chat_stays(self):
        chat = self.old_chat()
        chat.meta["default"] = True
        self.mgr.sync_tabs()
        self.assertEqual(self.news(), [])
        self.assertIn(chat.id, self.mgr.chats)

    def test_a_busy_chat_is_moved_when_it_is_done(self):
        chat = self.new()
        chat.send("slow")
        wait(lambda: chat.live)
        self.mgr.sync_tabs()
        self.assertEqual(self.news(), [])
        chat.interrupt()
        wait(lambda: chat.state == "idle")
        self.mgr.sync_tabs()
        self.assertEqual(len(self.news()), 1)

    def test_a_chat_on_its_way_into_a_tab_takes_no_message(self):
        chat = self.old_chat()
        seen = []

        def ops(op, target=None, body=None):         # the user sends while the tab opens
            if op == "new":
                seen.append(tc.api(self.mgr, "POST", [chat.id, "send"], {"text": "now"})[0])
            return self.tab_ops_was(op, target, body)
        self.tab_ops_was, self.mgr.tab_ops = self.mgr.tab_ops, ops
        self.mgr.sync_tabs()
        self.assertEqual(seen, [500])
        self.assertFalse(chat.running)

    def test_a_chat_that_will_not_move_is_left_after_a_few_tries(self):
        chat = self.old_chat()
        self.reply = (403, {"error": "managing tabs from the phone is off"})
        for _ in range(tc.MOVE_TRIES + 2):
            self.mgr.sync_tabs()
        self.assertEqual(len(self.news()), tc.MOVE_TRIES)
        self.assertIn(chat.id, self.mgr.chats)

    def test_a_room_keeps_its_chats_when_they_move_into_tabs(self):
        a, b = self.old_chat(title="A"), self.old_chat(title="B")
        code, out = self.mgr.create_group({"members": [a.id, b.id]})
        self.assertEqual(code, 200)
        group = self.mgr.groups[out["group"]["id"]]
        names = sorted(group.meta["alias"].values())
        self.mgr.sync_tabs()
        self.assertIn(group.id, self.mgr.groups)
        members = group.members()
        self.assertEqual(len(members), 2)
        self.assertTrue(all(c.mirrored for c in members))
        self.assertEqual(sorted(group.meta["alias"].values()), names)
        self.assertNotIn(a.id, group.meta["members"])

    def test_a_new_chat_opens_a_tab_in_the_group_and_mirrors_it(self):
        code, out = tc.api(self.mgr, "POST", [], {"kind": "tab", "cwd": self.dir, "title": "Run", "group": "blue", "emoji": "🏃"})
        self.assertEqual(code, 200, out)
        op, target, body = next(o for o in self.ops if o[0] == "new")
        self.assertEqual((body["kind"], body["cli"], body["group"], body["name"]), ("ai", "claude", "blue", "Run"))
        chat = self.mgr.get(out["chat"]["id"])
        self.assertEqual((chat.meta["tab"]["id"], chat.meta["emoji"]), ("t9", "🏃"))
        self.tabs.append({"id": "t9", "title": "MOT", "group": "Work", "color": "blue"})
        self.mgr.sync_tabs()
        self.assertEqual(len([c for c in self.mgr.chats.values() if c.mirrored and c.meta["tab"]["id"] == "t9"]), 1)

    def test_a_new_chat_opens_its_tab_with_the_login_picked(self):
        self.mgr.accounts = lambda: [("Work", FAKE)]
        tc.api(self.mgr, "POST", [], {"kind": "tab", "cwd": self.dir, "account": "Work"})
        self.assertEqual(next(o for o in self.ops if o[0] == "new")[2]["account"], "Work")
        self.ops.clear()
        tc.api(self.mgr, "POST", [], {"kind": "tab", "cwd": self.dir})      # none picked: the default
        self.assertEqual(next(o for o in self.ops if o[0] == "new")[2]["account"], "")
        self.ops.clear()
        code, _ = tc.api(self.mgr, "POST", [], {"kind": "tab", "cwd": self.dir, "account": "nobody"})
        self.assertEqual((code, self.ops), (400, []))

    def test_a_new_chat_can_open_a_codex_tab(self):
        self.mgr.accounts = lambda: [("Work", FAKE)]
        self.mgr.codex_accounts = lambda: [("Home", FAKE)]
        code, out = tc.api(self.mgr, "POST", [], {"kind": "tab", "cwd": self.dir, "cli": "codex",
                                                  "account": "Work"})
        self.assertEqual(code, 400, out)               # a Claude login is no Codex login
        code, out = tc.api(self.mgr, "POST", [], {"kind": "tab", "cwd": self.dir, "cli": "codex",
                                                  "account": "Home"})
        self.assertEqual(code, 200, out)
        body = next(o for o in self.ops if o[0] == "new")[2]
        self.assertEqual((body["cli"], body["account"]), ("codex", "Home"))
        self.ops.clear()
        tc.api(self.mgr, "POST", [], {"kind": "tab", "cwd": self.dir, "cli": "codex"})
        self.assertEqual(next(o for o in self.ops if o[0] == "new")[2]["account"], "")
        self.ops.clear()
        code, _ = tc.api(self.mgr, "POST", [], {"kind": "tab", "cwd": self.dir, "cli": "agy"})
        self.assertEqual((code, self.ops), (400, []))

    def test_a_new_chat_can_open_a_grok_tab(self):
        self.mgr.accounts = lambda: [("Work", FAKE)]
        code, out = tc.api(self.mgr, "POST", [], {"kind": "tab", "cwd": self.dir, "cli": "grok"})
        self.assertEqual(code, 200, out)
        body = next(o for o in self.ops if o[0] == "new")[2]
        self.assertEqual((body["cli"], body["account"]), ("grok", ""))
        self.ops.clear()
        code, _ = tc.api(self.mgr, "POST", [], {"kind": "tab", "cwd": self.dir, "cli": "grok",
                                                "account": "Work"})
        self.assertEqual((code, self.ops), (400, []))      # Grok's login is set in its tab

    def test_a_new_chat_can_go_on_with_a_conversation_picked(self):
        sid = "01a14318-e1a5-7552-8ddb-5427756b9cfd"
        tc.api(self.mgr, "POST", [], {"kind": "tab", "cwd": self.dir, "cli": "grok", "resume": sid})
        self.assertEqual(next(o for o in self.ops if o[0] == "new")[2]["resume"], sid)
        self.ops.clear()
        tc.api(self.mgr, "POST", [], {"kind": "tab", "cwd": self.dir})
        self.assertNotIn("resume", next(o for o in self.ops if o[0] == "new")[2])
        self.ops.clear()
        code, _ = tc.api(self.mgr, "POST", [], {"kind": "tab", "cwd": self.dir, "resume": "--continue"})
        self.assertEqual((code, self.ops), (400, []))

    def test_the_page_asks_for_a_folders_conversations(self):
        got = {}
        def ops(op, target=None, body=None):
            got[op] = body
            return 200, {"sessions": [{"id": "x", "title": "fix it", "ts": 1}]}
        self.mgr.tab_ops = ops
        code, out = tc.api(self.mgr, "POST", ["sessions"], {"cli": "codex", "cwd": "~/x"})
        self.assertEqual((code, out["sessions"][0]["title"]), (200, "fix it"))
        self.assertEqual(got["sessions"], {"cli": "codex", "cwd": os.path.expanduser("~/x")})
        code, _ = tc.api(self.mgr, "POST", ["sessions"], {"cli": "agy", "cwd": "~"})
        self.assertEqual(code, 400)


    def test_the_page_is_told_which_ais_there_are(self):
        code, out = tc.api(self.mgr, "GET", [], None)
        self.assertEqual(out["clis"][0], "claude")

    def test_a_new_chat_with_no_group_leaves_it_to_tabit(self):
        tc.api(self.mgr, "POST", [], {"kind": "tab", "cwd": self.dir})
        body = next(o for o in self.ops if o[0] == "new")[2]
        self.assertEqual(body["group"], "")

    def test_a_tab_tabit_will_not_open_makes_no_chat(self):
        self.reply = (403, {"error": "managing tabs from the phone is off"})
        code, out = tc.api(self.mgr, "POST", [], {"kind": "tab", "cwd": self.dir})
        self.assertEqual(code, 403)
        self.assertEqual(self.titles(), [])

    def test_the_list_says_which_groups_a_tab_can_go_in(self):
        code, out = tc.api(self.mgr, "GET", [], {})
        self.assertEqual(out["tabgroups"]["groups"][0]["name"], "Work")


class UsageTest(Base):
    """Each AI login the chats use, with how much of its limits is gone."""

    def test_a_claude_tab_s_usage_comes_from_its_status_line(self):
        self.mgr.roster_dir = os.path.join(self.dir, "hooks")
        os.makedirs(self.mgr.roster_dir)
        self.mgr.tab_hook = lambda tab, text=None, key=None, queue=False, screen=False: {
            "title": "A", "status": "idle", "transcript": "", "cli": "claude"}
        self.mgr.tabs_hook = lambda: [{"id": "t1", "title": "A", "cli": "claude",
                                       "account": "Personal", "key": "ai-claude-a"}]
        self.mgr.tab_ops = lambda *a, **k: (200, {"ok": True})
        self.mgr.sync_tabs()
        with open(os.path.join(self.mgr.roster_dir, "ai-claude-a.usage"), "w") as f:
            json.dump({"rate_limits": {"five_hour": {"used_percentage": 13, "resets_at": 1900000000},
                                       "seven_day": {"used_percentage": 36.6, "resets_at": 1900500000}},
                       "ts": 1800000000}, f)
        self.mgr.create({"cwd": self.dir, "title": "own"})       # a chat with its own claude: Default
        got = {(u["cli"], u["account"]): u["windows"] for u in tc.api(self.mgr, "GET", [], None)[1]["usage"]}
        self.assertEqual(list(got), [("claude", ""), ("claude", "Personal")])   # Default first
        self.assertEqual(got[("claude", "Personal")], [{"name": "5h", "pct": 13, "resets": 1900000000},
                                                       {"name": "7d", "pct": 37, "resets": 1900500000}])
        self.assertEqual(got[("claude", "")], [])                # not known yet

    def test_a_claude_login_with_no_fresh_reading_is_asked(self):
        asked = []
        self.mgr.claude_usage = lambda acct: asked.append(acct) or [{"name": "5h", "pct": 1, "resets": 0}]
        self.mgr.create({"cwd": self.dir, "title": "own"})
        self.mgr.usage()
        self.assertTrue(wait(lambda: self.mgr._usage.get("claude/")))
        with self.mgr.lock:          # its file is written under the lock: done when it is free
            pass
        self.assertEqual(self.mgr.usage()[0]["windows"], [{"name": "5h", "pct": 1, "resets": 0}])
        self.assertEqual(asked, [""])                  # once: the reading is fresh now

    def test_an_older_reading_does_not_win(self):
        self.mgr.note_usage("codex", "", [{"name": "5h", "pct": 50, "resets": 0}], 200)
        self.mgr.note_usage("codex", "", [{"name": "5h", "pct": 10, "resets": 0}], 100)
        self.assertEqual(self.mgr._usage["codex/"]["windows"][0]["pct"], 50)


class TabSwitchTest(Base):
    """The login and the model of a chat, from its page."""

    def setUp(self):
        super().setUp()
        self.addCleanup(setattr, tc, "MIRROR_EVERY", tc.MIRROR_EVERY)
        tc.MIRROR_EVERY = 3600
        self.typed, self.ops, self.busy = [], [], False
        self.path = os.path.join(self.dir, "tx.jsonl")
        self.mgr.accounts = lambda: [("Personal", FAKE)]

        def hook(tab, text=None, key=None, queue=False, screen=False):
            if text is None and key is None:
                return {"title": "MOT", "status": "idle", "transcript": self.path, "cli": "claude"}
            if self.busy:
                return 409, {"error": "that tab is busy: wait for it"}
            self.typed.append(text)
            return 200, {"ok": True}
        self.mgr.tab_hook = hook
        def ops(op, target=None, body=None):
            self.ops.append((op, target, body))
            if op == "model":
                return (409, {"error": "that tab is busy: wait for it"}) if self.busy else (200, {"ok": True})
            return 200, {"ok": True}
        self.mgr.tab_ops = ops
        code, out = self.mgr.create({"tab": {"id": "t1", "title": "MOT"}})
        self.chat = self.mgr.get(out["chat"]["id"])

    def edit(self, body):
        return tc.api(self.mgr, "POST", [self.chat.id, "edit"], body)

    def test_the_model_is_switched_by_tabit(self):
        self.assertEqual(self.edit({"model": "sonnet"})[0], 200)
        self.assertEqual(self.ops, [("model", "t1", {"model": "sonnet"})])
        self.assertEqual(model := tc.model_alias(self.chat.meta["tab"]["model"]), "sonnet")

    def test_default_model_and_no_change(self):
        self.chat.meta["tab"]["model"] = "claude-opus-4-5"
        self.edit({"model": "opus"})
        self.assertEqual(self.ops, [])
        self.edit({"model": ""})
        self.assertEqual(self.ops, [("model", "t1", {"model": ""})])

    def test_a_busy_tab_keeps_its_model(self):
        self.busy = True
        code, out = self.edit({"model": "haiku"})
        self.assertEqual(code, 409)
        self.assertNotIn("model", self.chat.meta["tab"])

    def test_a_model_nobody_knows(self):
        self.assertEqual(self.edit({"model": "gpt"})[0], 400)
        self.assertEqual(self.ops, [])

    def test_the_effort_is_switched_by_tabit(self):
        self.assertEqual(self.edit({"effort": "ultra"})[0], 400)    # not a Claude effort
        self.assertEqual(self.edit({"effort": "max"})[0], 200)
        self.assertEqual(self.ops, [("model", "t1", {"effort": "max"})])
        self.assertEqual(self.chat.summary()["tab"]["effort"], "max")
        self.edit({"effort": "max"})
        self.assertEqual(len(self.ops), 1)          # no change: nothing asked

    def test_login_and_model_change_in_one_restart(self):
        self.assertEqual(self.edit({"account": "Personal", "model": "haiku"})[0], 200)
        self.assertEqual(self.ops, [("model", "t1", {"model": "haiku", "account": "Personal"})])
        self.assertEqual(self.chat.summary()["account"], "Personal")

    def test_the_login_is_switched_by_tabit(self):
        self.assertEqual(self.edit({"account": "Personal"})[0], 200)
        self.assertEqual(self.ops, [("account", "t1", {"account": "Personal"})])
        self.assertEqual(self.chat.summary()["account"], "Personal")
        self.assertEqual(self.edit({"account": "Nobody"})[0], 400)
        self.edit({"account": "Personal"})
        self.assertEqual(len(self.ops), 1)          # no change: nothing asked

    def test_the_model_that_answered_last_shows(self):
        with open(self.path, "w") as f:
            f.write(json.dumps({"type": "assistant", "message": {"role": "assistant", "model": "claude-haiku-4-5",
                                "content": [{"type": "text", "text": "hi"}]}}) + "\n")
            f.write(json.dumps({"type": "assistant", "message": {"role": "assistant", "model": "<synthetic>",
                                "content": [{"type": "text", "text": "x"}]}}) + "\n")
        self.chat.mirror_poll()
        self.assertEqual(self.chat.meta["tab"]["model"], "claude-haiku-4-5")

    def test_the_model_is_found_in_the_end_of_an_old_transcript(self):
        with open(self.path, "w") as f:
            for m in ("claude-opus-5-5", "claude-sonnet-5-5"):
                f.write(json.dumps({"type": "assistant", "message": {"model": m, "content": []}}) + "\n")
        self.chat.meta["mirror"] = {"path": self.path, "offset": os.path.getsize(self.path)}   # all read already
        self.chat.mirror_poll()
        self.assertEqual(self.chat.meta["tab"]["model"], "claude-sonnet-5-5")

    def test_a_chat_with_its_own_claude_has_its_own_login(self):
        own = self.new()
        self.assertEqual(tc.api(self.mgr, "POST", [own.id, "edit"], {"account": "Personal"})[0], 200)
        self.assertEqual(own.login(), "Personal")
        self.assertEqual(own._argv()[0], FAKE)
        self.assertEqual(self.mgr.account, "")      # the shared login did not move


TRUST_SCREEN = ["", " Accessing workspace:", "", " /home/chris/strava", "",
                " Quick safety check: Is this a project you trust?", "",
                " ❯ No, exit", "   Yes, I trust this folder", "", " Enter to confirm · Esc to cancel"]
PERMISSION_SCREEN = [" Do you want to create a.txt?", " ❯ 1. Yes", "   2. Yes, allow all edits", "   3. No (esc)", ""]


class ScreenMenuTest(Base):
    """A tab that waits on a menu (trust, permission): the chat shows it as a card."""

    def setUp(self):
        super().setUp()
        self.addCleanup(setattr, tc, "MIRROR_EVERY", tc.MIRROR_EVERY)
        self.addCleanup(setattr, tc, "ANSWER_STEP", tc.ANSWER_STEP)
        tc.MIRROR_EVERY = 3600
        tc.ANSWER_STEP = 0
        self.screen, self.status, self.keys = TRUST_SCREEN, "blocked", []

        def hook(tab, text=None, key=None, queue=False, screen=False):
            if text is None and key is None:
                info = {"title": "MOT", "status": self.status, "transcript": "", "cli": "claude"}
                if self.status == "blocked":
                    info["screen"] = self.screen
                return info
            self.keys.append(key)
            return 200, {"ok": True}
        self.mgr.tab_hook = hook
        code, out = self.mgr.create({"tab": {"id": "t1", "title": "MOT"}})
        self.chat = self.mgr.get(out["chat"]["id"])

    def card(self):
        self.chat.mirror_poll()
        return next(iter(self.chat.view()["pending"]), None)

    def codex_screen(self):
        with open(os.path.join(os.path.dirname(__file__), "fixtures", "chat",
                               "codex_approval_screen.txt")) as f:
            return f.read().splitlines()

    def test_the_last_choice_picked_under_a_long_one(self):
        rows = [("  " + r[2:]) if r.startswith("› 1.") else
                ("› " + r.lstrip()) if r.lstrip().startswith("3. No") else r
                for r in self.codex_screen()]
        menu = tc.screen_prompt(rows)
        self.assertEqual(menu["numbers"], [1, 2, 3])        # the long 2nd one does not cut the menu
        self.assertEqual(menu["selected"], 2)               # the 3rd choice, not the 4th row

    def test_a_codex_approval_is_read(self):
        menu = tc.screen_prompt(self.codex_screen())
        self.assertEqual(menu["question"], "Would you like to run the following command?")
        self.assertEqual(menu["numbers"], [1, 2, 3])
        self.assertEqual(menu["options"], [
            "Yes, proceed (y)",
            "Yes, and don't ask again for commands that start with `echo hi > note.txt` (p)",
            "No, and tell Codex what to do differently (esc)"])
        self.assertEqual(menu["selected"], 0)
        # the prompt from its question down; the talk above it is not part of it
        self.assertTrue(menu["detail"].startswith("Would you like"), menu["detail"])
        self.assertIn("$ echo hi > note.txt", menu["detail"])
        self.assertNotIn("AGENTS.md", menu["detail"])

    def test_the_codex_box_is_no_menu(self):
        self.assertIsNone(tc.screen_prompt(["› 1. fix it", "", "  gpt-5.6 medium · ~/p"]))
        self.assertIsNone(tc.screen_prompt(["• Done", "", "› Ask Codex to do anything", ""]))

    def test_a_codex_approval_is_answered_with_its_number(self):
        self.screen = self.codex_screen()
        ev = self.card()
        q = ev["questions"][0]["question"]
        self.assertTrue(self.chat.answer(ev["rid"], True, answers={q: "Yes, proceed (y)"}))
        self.assertEqual(self.keys, ["1"])
        self.keys.clear()
        self.status = "working"                          # it ran; then the same question again
        self.chat.mirror_poll()
        self.status = "blocked"
        ev = self.card()
        self.chat.answer(ev["rid"], False)
        self.assertEqual(self.keys, ["3"])               # its No

    def test_the_trust_question_is_a_card(self):
        ev = self.card()
        self.assertEqual([o["label"] for o in ev["questions"][0]["options"]],
                         ["No, exit", "Yes, I trust this folder"])
        self.assertIn("Quick safety check", ev["questions"][0]["question"])
        self.assertEqual(len(self.chat.view()["pending"]), 1)
        self.chat.mirror_poll()
        self.assertEqual(len(self.chat.view()["pending"]), 1)        # not made twice

    def test_a_menu_without_numbers_is_walked_with_arrows(self):
        ev = self.card()
        q = ev["questions"][0]["question"]
        self.assertTrue(self.chat.answer(ev["rid"], True, answers={q: "Yes, I trust this folder"}))
        self.assertEqual(self.keys, ["down", "enter"])

    def test_the_chosen_row_needs_no_arrow(self):
        ev = self.card()
        q = ev["questions"][0]["question"]
        self.chat.answer(ev["rid"], True, answers={q: "No, exit"})
        self.assertEqual(self.keys, ["enter"])

    def test_a_numbered_menu_takes_its_number(self):
        self.screen = PERMISSION_SCREEN
        ev = self.card()
        q = ev["questions"][0]["question"]
        self.chat.answer(ev["rid"], True, answers={q: "Yes, allow all edits"})
        self.assertEqual(self.keys, ["2"])

    def test_skip_leaves_the_trust_question_to_the_tab(self):
        # its No and its Esc both end Claude: a skip presses nothing
        ev = self.card()
        self.chat.answer(ev["rid"], False)
        self.assertEqual(self.keys, [])
        self.assertIsNone(self.card())                  # not asked again while it waits

    def test_skip_on_a_permission_question_picks_no(self):
        self.screen = PERMISSION_SCREEN
        ev = self.card()
        self.chat.answer(ev["rid"], False)
        self.assertEqual(self.keys, ["3"])

    def test_the_whole_prompt_shows_however_long(self):
        cmd = ["   line %d of a long command" % i for i in range(12)]
        self.screen = ["────────", " Bash command", ""] + cmd + ["", " Do you want to proceed?",
                       " ❯ 1. Yes", "   2. No (esc)", ""]
        ev = self.card()
        self.assertEqual(ev["questions"][0]["question"], "Do you want to proceed?")
        for line in cmd:
            self.assertIn(line.strip(), ev["detail"])
        self.assertNotIn("cut", ev["detail"])

    def test_a_prompt_taller_than_the_screen_says_so(self):
        self.screen = ["  more of it %d" % i for i in range(40)] + [" Do you want to proceed?",
                                                                     " ❯ 1. Yes", "   2. No"]
        self.assertIn("open the tab", self.card()["detail"])

    def test_every_key_waits_for_the_menu_to_be_there(self):
        ev = self.card()
        q = ev["questions"][0]["question"]
        hook = self.mgr.tab_hook

        def gone_after_one(tab, text=None, key=None, queue=False):
            out = hook(tab, text, key, queue)
            if key is not None:
                self.status = "idle"                    # answered at the desk meanwhile
            return out
        self.mgr.tab_hook = gone_after_one
        with self.assertRaises(tc._Gone):
            self.chat.answer(ev["rid"], True, answers={q: "Yes, I trust this folder"})
        self.assertEqual(self.keys, ["down"])

    def test_a_menu_that_is_gone_presses_nothing(self):
        ev = self.card()
        q = ev["questions"][0]["question"]
        self.status = "idle"
        with self.assertRaises(tc._Gone):
            self.chat.answer(ev["rid"], True, answers={q: "No, exit"})
        self.assertEqual(self.keys, [])

    def test_the_card_goes_when_the_tab_goes_on(self):
        self.card()
        self.status = "idle"
        self.chat.mirror_poll()
        self.assertEqual(self.chat.view()["pending"], [])

    def test_the_same_menu_asked_again_is_a_card_again(self):
        ev = self.card()
        q = ev["questions"][0]["question"]
        self.chat.answer(ev["rid"], True, answers={q: "No, exit"})
        self.assertIsNone(self.card())                  # still on screen: not asked twice
        self.status = "idle"
        self.chat.mirror_poll()
        self.status = "blocked"
        self.assertEqual(self.card()["rid"], ev["rid"])

    def test_a_blocked_tab_with_no_menu_makes_no_card(self):
        self.screen = ["────", "❯ hello", "────"]
        self.assertIsNone(self.card())

    def test_a_hint_under_a_numbered_menu_is_not_a_choice(self):
        menu = tc.screen_prompt([" Do you want to run it?", " ❯ 1. Yes", "   2. No", "   Esc to cancel"])
        self.assertEqual(menu["options"], ["Yes", "No"])

    def test_the_parser(self):
        self.assertIsNone(tc.screen_prompt(None))
        self.assertEqual(tc.screen_prompt(PERMISSION_SCREEN)["numbers"], [1, 2, 3])
        self.assertIsNone(tc.screen_prompt([" ❯ a", "   b"]))          # two rows, no number, no "Enter to confirm"


class TabTalkTest(Base):
    """A Claude tab's reply line that starts with @name is typed into that tab."""

    def setUp(self):
        super().setUp()
        self.addCleanup(setattr, tc, "MIRROR_EVERY", tc.MIRROR_EVERY)
        tc.MIRROR_EVERY = 3600
        self.tx = {}
        for t in ("ta", "tb"):
            self.tx[t] = os.path.join(self.dir, t + ".jsonl")
            shutil.copy(os.path.join(HERE, "fixtures", "chat", "tab_transcript.jsonl"), self.tx[t])
        self.titles = {"ta": "Alpha", "tb": "Beta"}
        self.tabs = [{"id": "ta", "title": "Alpha", "group": "", "color": "", "key": "ai-a"},
                     {"id": "tb", "title": "Beta", "group": "Work", "color": "blue", "key": "ai-b"}]
        self.typed, self.refuse = [], set()

        def hook(tab, text=None, key=None, queue=False, screen=False):
            if text is None and key is None:
                return {"title": self.titles[tab], "status": "idle", "transcript": self.tx[tab], "cli": "claude"}
            if tab in self.refuse:
                return 409, {"error": "that tab is asking something"}
            self.typed.append((tab, text))
            return 200, {"ok": True}
        self.mgr.tab_hook = hook
        self.mgr.tabs_hook = lambda: [dict(t) for t in self.tabs]
        self.mgr.tab_ops = lambda op, target=None, body=None: (200, {"ok": True})
        self.mgr.roster_dir = os.path.join(self.dir, "hooks")
        os.makedirs(self.mgr.roster_dir)
        self.mgr.sync_tabs()
        self.a, self.b = (next(c for c in self.mgr.chats.values() if c.mirrored and c.meta["tab"]["id"] == t)
                          for t in ("ta", "tb"))
        self.a.mirror_poll()
        self.b.mirror_poll()

    def say(self, tab, text):
        with open(self.tx[tab], "a") as f:
            f.write(json.dumps({"type": "assistant", "message": {"content": [
                {"type": "text", "text": text}]}}) + "\n")
        (self.a if tab == "ta" else self.b).mirror_poll()

    def test_a_line_starting_with_at_name_goes_to_that_tab(self):
        self.say("ta", "Let me ask.\n@beta: can you check the build?")
        self.assertEqual(self.typed, [("tb", "[from @alpha]: Let me ask.\n@beta: can you check the build?")])
        self.assertTrue(any(e["t"] == "relay" and e["to"] == "beta" for e in self.a.events))
        self.say("tb", "@alpha: it passes")                   # and back
        self.assertEqual(self.typed[-1], ("ta", "[from @beta]: @alpha: it passes"))

    def test_no_mention_no_name_and_no_self_send_nothing(self):
        self.say("ta", "all done, nobody to ask")
        self.say("ta", "@nobody: hello")
        self.say("ta", "@alpha: talking to myself")
        self.assertEqual(self.typed, [])

    def test_the_past_is_not_sent(self):
        with open(self.tx["ta"], "a") as f:
            f.write(json.dumps({"type": "assistant", "message": {"content": [
                {"type": "text", "text": "@beta: an old line"}]}}) + "\n")
        self.a.meta["mirror"] = {}                            # a first look again: the past
        self.a.mirror_poll()
        self.assertEqual(self.typed, [])

    def test_a_tab_that_cannot_take_it_says_so_in_the_sender(self):
        self.refuse.add("tb")
        self.say("ta", "@beta: hello")
        self.assertTrue(any(e["t"] == "error" and "beta" in e["text"] for e in self.a.events))

    def test_tabs_in_one_group_leave_it_to_the_group(self):
        code, out = self.mgr.create_group({"members": [self.a.id, self.b.id]})
        self.assertEqual(code, 200, out)
        group = self.mgr.groups[out["group"]["id"]]
        group.send("@alpha ask beta")
        with open(self.tx["ta"], "a") as f:                  # the group's question reaches the tab
            f.write(json.dumps({"type": "user", "message": {"content": self.typed[-1][1]}}) + "\n")
        self.say("ta", "@beta: hello")
        to_b = [t[1] for t in self.typed if t[0] == "tb"]
        self.assertEqual(len(to_b), 1)                      # once, through the group
        self.assertTrue(to_b[0].endswith("[alpha]: @beta: hello"))
        self.assertTrue(any(e["t"] == "relay" for e in group.events))

    def test_a_bracket_typed_at_the_desk_is_no_group_line(self):
        code, out = self.mgr.create_group({"members": [self.a.id, self.b.id]})
        group = self.mgr.groups[out["group"]["id"]]
        with open(self.tx["ta"], "a") as f:
            f.write(json.dumps({"type": "user", "message": {"content": "[WIP]: tidy the parser"}}) + "\n")
        self.say("ta", "@beta: can you look at this?")
        self.assertEqual([e for e in group.events if e["t"] == "say"], [])
        self.assertEqual(self.typed, [("tb", "[from @alpha]: @beta: can you look at this?")])

    def test_a_groups_name_is_its_member_not_a_tab_of_the_same_name(self):
        self.tabs.append({"id": "tc", "title": "Gamma 2", "group": "", "color": "", "key": "ai-c"})
        self.titles["tc"] = "Gamma 2"
        self.tx["tc"] = self.tx["tb"]
        self.mgr.sync_tabs()                                # a tab named gamma-2
        other = self.new(title="Gamma")
        code, out = self.mgr.create_group({"members": [self.a.id, self.b.id, other.id]})
        group = self.mgr.groups[out["group"]["id"]]
        group.meta["alias"][self.b.id] = "gamma-2"         # and the group calls beta so
        self.say("ta", "@gamma-2: hello")
        self.assertEqual([t[0] for t in self.typed], ["tb"])
        self.say("ta", "@gamma: hello")                      # a member with no tab
        self.assertTrue(any(e["t"] == "error" and "not a tab" in e["text"] for e in self.a.events))

    def test_tabs_in_one_group_talk_directly_when_asked_alone(self):
        code, out = self.mgr.create_group({"members": [self.a.id, self.b.id]})
        group = self.mgr.groups[out["group"]["id"]]
        self.say("ta", "@beta: hello")                       # asked at the desk: not the group's
        self.assertEqual([t for t in self.typed if t[0] == "tb"], [("tb", "[from @alpha]: @beta: hello")])
        self.assertTrue(any(e["t"] == "relay" and e["to"] == "beta" for e in self.a.events))
        self.assertEqual([e for e in group.events if e["t"] in ("say", "relay")], [])

    def test_tabs_of_one_name_are_told_apart_by_their_group(self):
        for c, group in ((self.a, "EHR330"), (self.b, "Work")):
            c.meta["title"] = "claude"
            c.meta["tab"]["group"] = group
        names = sorted(self.mgr.tab_names())
        self.assertEqual(names, ["ehr330-claude", "work-claude"])

    def test_a_file_a_code_block_or_a_mention_is_no_call(self):
        self.say("ta", "@beta.py is the file\n@beta/x.md too")
        self.say("ta", "Like this:\n```\n@beta: hello\n```\ndone")
        self.assertEqual(self.typed, [])
        self.say("ta", "@beta, please look")              # only a mention: no colon
        self.say("ta", "@beta 同意我的 review。")
        self.assertEqual(self.typed, [])
        self.say("ta", "@beta: and this")
        self.say("ta", "@beta：好")                          # a Chinese colon too
        self.assertEqual(len(self.typed), 2)
        self.assertEqual(tc.mentions("@beta，請看"), [])
        self.assertEqual(tc.mentions("@alpha, @beta：請看"), ["alpha", "beta"])

    def test_the_same_file_by_another_path_is_no_new_conversation(self):
        link = os.path.join(self.dir, "acct")
        os.symlink(self.dir, link)                  # an account's config dir: projects is a link
        before = len(self.a.events)
        self.a._mirror_read(os.path.join(link, os.path.basename(self.tx["ta"])))
        self.assertEqual(len(self.a.events), before)
        self.say("ta", "@beta: still here")
        self.assertEqual(len(self.typed), 1)

    def send(self, chat, text):
        return tc.api(self.mgr, "POST", [chat.id, "send"], {"text": text})

    def test_me_and_another_is_one_bubble(self):
        self.send(self.a, "@alpha @beta compare")
        self.assertEqual(sorted(self.typed), [("ta", "compare"), ("tb", "compare")])
        self.assertFalse([e for e in self.a.events if e["t"] == "user" and e.get("to")])

    def test_a_long_group_message_reaches_a_tab_cut(self):
        code, out = self.mgr.create_group({"members": [self.a.id, self.b.id]})
        group = self.mgr.groups[out["group"]["id"]]
        group.send("@beta " + "y" * 9000)
        [(tab, text)] = self.typed
        self.assertEqual(tab, "tb")
        self.assertLessEqual(len(text), 8000)

    def test_a_file_name_first_goes_to_the_chat(self):
        self.send(self.a, "@beta.py fix this")
        self.assertEqual(self.typed, [("ta", "@beta.py fix this")])

    def test_the_user_at_names_send_to_those_tabs_only(self):
        self.assertEqual(self.send(self.a, "@beta can you check the build?")[0], 200)
        self.assertEqual(self.typed, [("tb", "can you check the build?")])
        self.assertTrue(any(e["t"] == "user" and e.get("to") == ["beta"] for e in self.a.events))
        self.typed.clear()
        self.assertEqual(self.send(self.a, "@alpha, @beta hello")[0], 200)
        self.assertEqual(sorted(self.typed), [("ta", "hello"), ("tb", "hello")])

    def test_no_known_name_first_the_chat_takes_it(self):
        self.send(self.a, "@nobody hello")
        self.send(self.a, "ask @beta later")
        self.assertEqual(self.typed, [("ta", "@nobody hello"), ("ta", "ask @beta later")])

    def test_a_tab_that_cannot_take_it_is_said(self):
        self.refuse.add("tb")
        code, out = self.send(self.a, "@beta hello")
        self.assertNotEqual(code, 200)
        self.assertIn("beta", out["error"])

    def test_the_list_gives_each_tab_its_name_for_the_text_box(self):
        at = {r["id"]: r.get("at") for r in self.mgr.listing()}
        self.assertEqual((at[self.a.id], at[self.b.id]), ("alpha", "beta"))
        self.assertTrue(all(at[c.id] is None for c in self.mgr.chats.values() if not c.mirrored))

    def roster(self, key):
        path = os.path.join(self.mgr.roster_dir, key + ".roster")
        if not os.path.exists(path):
            return None
        with open(path) as f:
            return json.load(f)["hookSpecificOutput"]

    def test_the_roster_tells_each_tab_who_is_there(self):
        a = self.roster("ai-a")
        self.assertEqual(a["hookEventName"], "UserPromptSubmit")
        self.assertIn("@alpha", a["additionalContext"])
        self.assertIn("@beta", a["additionalContext"])
        self.assertIn("@beta", self.roster("ai-b")["additionalContext"])
        path = os.path.join(self.mgr.roster_dir, "ai-a.roster")
        before = os.stat(path).st_mtime_ns
        time.sleep(0.02)
        self.mgr.sync_tabs()
        self.assertEqual(os.stat(path).st_mtime_ns, before)         # unchanged: not written again
        self.tabs.pop()                                              # Beta closes
        for _ in range(tc.GONE_AFTER + 1):
            self.mgr.sync_tabs()
        self.assertIsNone(self.roster("ai-b"))
        self.assertIsNone(self.roster("ai-a"))                       # alone: nobody to talk to

    def test_a_roster_left_from_before_a_restart_goes(self):
        old = os.path.join(self.mgr.roster_dir, "ai-old.roster")
        with open(old, "w") as f:
            f.write("{}")
        self.mgr._rosters.clear()                                    # a new tabit knows nothing
        self.mgr.sync_tabs()
        self.assertFalse(os.path.exists(old))
        self.assertIsNotNone(self.roster("ai-a"))

    def test_a_long_reply_reaches_the_other_tab_cut(self):
        self.say("ta", "@beta: here it is\n" + "x" * 9000)
        [(tab, text)] = self.typed
        self.assertEqual(tab, "tb")
        self.assertLessEqual(len(text), 8000)
        self.assertTrue(text.endswith("(cut)"))


class ReplyTest(Base):
    def ask(self, chat, text, **body):
        code, out = tc.api(self.mgr, "POST", [chat.id, "send"], dict({"text": text}, **body))
        self.assertEqual(code, 200, out)

    def test_a_reply_is_kept_with_a_short_quote_and_claude_is_told(self):
        chat = self.new()
        self.ask(chat, "first thing")
        wait(lambda: "done" in self.kinds(chat))
        first = next(e for e in chat.events if e["t"] == "user")
        self.ask(chat, "about that", reply=first["seq"])
        wait(lambda: self.kinds(chat).count("done") == 2)
        mine = [e for e in chat.events if e["t"] == "user"][1]
        self.assertEqual(mine["text"], "about that")
        self.assertEqual(mine["reply"], {"seq": first["seq"], "who": "You", "snip": "first thing"})
        said = [e["text"] for e in chat.events if e["t"] == "text"][-1]
        self.assertEqual(said, 'echo: [In reply to You: "first thing"]\nabout that')

    def test_a_claude_message_can_be_replied_to_and_a_bad_target_is_ignored(self):
        chat = self.new()
        self.ask(chat, "hello")
        wait(lambda: "done" in self.kinds(chat))
        answer = next(e for e in chat.events if e["t"] == "text")
        self.assertEqual(chat.quote(answer["seq"])["who"], "Claude")
        for bad in (None, "x", True, 0, 9999, 1.5):
            self.assertIsNone(chat.quote(bad), bad)
        self.assertIsNone(chat.quote(next(e["seq"] for e in chat.events if e["t"] == "done")))
        self.ask(chat, "again", reply=9999)
        wait(lambda: self.kinds(chat).count("done") == 2)
        self.assertNotIn("reply", [e for e in chat.events if e["t"] == "user"][1])

    def test_a_reply_in_a_group_reaches_the_members(self):
        a, b = self.new(title="aa"), self.new(title="bb")
        code, out = self.mgr.create_group({"members": [a.id, b.id]})
        group = self.mgr.groups[out["group"]["id"]]
        group.send("start")
        wait(lambda: len([e for e in group.events if e["t"] == "say"]) == 2)
        say = next(e for e in group.events if e["t"] == "say")
        code, _ = tc.group_api(self.mgr, "POST", [group.id, "send"],
                               {"text": "@aa more please", "reply": say["seq"]})
        self.assertEqual(code, 200)
        mine = [e for e in group.events if e["t"] == "user"][-1]
        self.assertEqual(mine["reply"]["who"], say["name"])
        wait(lambda: any("In reply to" in (e.get("text") or "") for e in a.events if e["t"] == "text"))


class TranscriptTimeTest(unittest.TestCase):
    def test_event_keeps_the_time_of_its_line(self):
        raw = {"type": "user", "timestamp": "2026-11-01T06:05:30.500Z", "message": {"content": "hi"}}
        [ev] = tc.transcript_events(raw)
        self.assertEqual(ev["ts"], 1793513130.5)

    def test_line_without_time_gets_none(self):
        [ev] = tc.transcript_events({"type": "user", "message": {"content": "hi"}})
        self.assertNotIn("ts", ev)


class QueuedMessageTest(unittest.TestCase):
    """A message sent while Claude works reaches it inside the turn, as an
    attachment line, not a user line: the chat still shows it."""

    def line(self, prompt, mode="prompt", kind="human"):
        return {"type": "attachment", "timestamp": "2026-11-04T12:28:03.470Z",
                "attachment": {"type": "queued_command", "prompt": prompt,
                               "commandMode": mode, "origin": {"kind": kind}}}

    def test_a_message_sent_mid_turn_shows(self):
        [ev] = tc.transcript_events(self.line("Also say SECOND"))
        self.assertEqual((ev["t"], ev["text"]), ("user", "Also say SECOND"))

    def test_notes_that_are_not_typed_do_not(self):
        self.assertEqual(tc.transcript_events(self.line("<task-notification>x")), [])
        self.assertEqual(tc.transcript_events(self.line("x", mode="task-notification")), [])
        self.assertEqual(tc.transcript_events(self.line("x", kind="task")), [])
        self.assertEqual(tc.transcript_events({"type": "attachment", "attachment": {"type": "date"}}), [])


class HeldLineTest(Base):
    """A line sent while the tab's Claude works waits in Claude Code's queue:
    the chat shows it as queued, and it can be taken back until Claude reads it."""

    HINT = "❯ Press up to edit queued messages"

    def setUp(self):
        super().setUp()
        self.addCleanup(setattr, tc, "MIRROR_EVERY", tc.MIRROR_EVERY)
        tc.MIRROR_EVERY = 3600
        self.path = os.path.join(self.dir, "tx.jsonl")
        open(self.path, "w").close()
        self.screen = ["─" * 20, self.HINT, "─" * 20]
        self.keys, self.typed = [], []
        self.pops = True             # Up brings the held lines back

        def hook(tab, text=None, key=None, queue=False, screen=False):
            if text is None and key is None:
                info = {"title": "MOT", "status": "working", "transcript": self.path, "cli": "claude"}
                if screen:
                    info["screen"] = self.screen
                return info
            if key:
                self.keys.append(key)
                if key == "up" and self.pops:
                    for t in self.held():
                        self.op("popAll", t)
            else:
                self.typed.append(text)
                self.op("enqueue", text)
            return 200, {"ok": True}
        self.mgr.tab_hook = hook
        code, out = self.mgr.create({"tab": {"id": "t1", "title": "x"}})
        self.chat = self.mgr.get(out["chat"]["id"])
        self.chat.mirror_poll()

    def op(self, operation, text=None):
        line = {"type": "queue-operation", "operation": operation, "sessionId": "s"}
        if text is not None:
            line["content"] = text
        with open(self.path, "a") as f:
            f.write(json.dumps(line) + "\n")

    def held(self):
        return self.chat._live_queue(self.path)[0]

    def shown(self):
        self.chat.mirror_poll()
        return [q["text"] for q in self.chat.view()["queued"]]

    def test_the_queue_follows_claude_code(self):
        q = []
        for op, text in (("enqueue", "a"), ("enqueue", "b"), ("enqueue", "c"),
                         ("dequeue", None), ("remove", "c")):
            tc.queue_step(q, {"type": "queue-operation", "operation": op, "content": text})
        self.assertEqual(q, ["b"])
        tc.queue_step(q, {"type": "queue-operation", "operation": "popAll", "content": "b"})
        self.assertEqual(q, [])
        self.assertFalse(tc.queue_step(q, {"type": "user"}))

    def test_held_lines_show_until_claude_reads_them(self):
        self.op("enqueue", "first")
        self.op("enqueue", "<task-notification>x")       # Claude's own note
        self.op("enqueue", "[from @lpr] hi")              # another tab's
        self.assertEqual(self.shown(), ["first"])
        self.op("remove", "first")                        # read mid-turn
        self.assertEqual(self.shown(), [])

    def test_a_held_line_is_taken_back_and_the_others_typed_again(self):
        self.op("enqueue", "keep one")
        self.op("enqueue", "take me back")
        self.op("enqueue", "keep two")
        self.chat.mirror_poll()
        key = [q["key"] for q in self.chat.view()["queued"] if q["text"] == "take me back"][0]
        code, out = self.chat.unqueue(key)
        self.assertEqual((code, out.get("text")), (200, "take me back"))
        self.assertEqual(self.keys[0], "up")
        self.assertEqual(self.keys[-1], "ctrl-u")         # the box is emptied before typing
        self.assertEqual(self.typed, ["keep one", "keep two"])
        self.assertEqual(self.shown(), ["keep one", "keep two"])

    def test_a_line_already_read_is_not_taken_back(self):
        self.op("enqueue", "late")
        self.chat.mirror_poll()
        key = self.chat.view()["queued"][0]["key"]
        self.op("remove", "late")
        self.assertEqual(self.chat.unqueue(key)[0], 409)
        self.assertEqual(self.keys, [])

    def test_text_in_the_box_stops_it(self):
        # Up would move in the person's own draft: leave it to the tab
        self.op("enqueue", "x")
        self.chat.mirror_poll()
        self.screen = ["─" * 20, "❯ my draft", "─" * 20]
        self.assertEqual(self.chat.unqueue(self.chat.view()["queued"][0]["key"])[0], 409)
        self.assertEqual(self.keys, [])

    def test_read_just_before_up_types_nothing_again(self):
        self.op("enqueue", "x")
        self.chat.mirror_poll()
        self.pops = False            # Claude took it in: Up only shows an old line
        self.addCleanup(setattr, tc, "UNQUEUE_WAIT", tc.UNQUEUE_WAIT)
        tc.UNQUEUE_WAIT = 0.3
        self.assertEqual(self.chat.unqueue(self.chat.view()["queued"][0]["key"])[0], 409)
        self.assertEqual(self.keys[-1], "ctrl-u")         # the old line is cleared away
        self.assertEqual(self.typed, [])

    def test_read_as_up_is_pressed_is_not_typed_again(self):
        # Claude takes the lines in at that moment: they leave its queue as
        # "remove", not "popAll", and must not be sent twice
        self.op("enqueue", "x")
        self.op("enqueue", "y")
        self.chat.mirror_poll()
        self.pops = False
        real = self.mgr.tab_hook

        def hook(tab, text=None, key=None, queue=False, screen=False):
            if key == "up":
                self.op("remove", "x")
                self.op("remove", "y")
            return real(tab, text, key, queue, screen)
        self.mgr.tab_hook = hook
        key = self.chat.view()["queued"][0]["key"]
        self.assertEqual(self.chat.unqueue(key)[0], 409)
        self.assertEqual(self.typed, [])


class SlashCommandTest(Base):
    """Typing / in a tab's chat lists the AI's commands and skills; a command
    the tab ran shows as the user's line, with what it printed."""

    def test_a_command_and_its_output_show(self):
        name = {"type": "system", "subtype": "local_command",
                "content": "<command-name>/context</command-name>\n  <command-message>context"
                           "</command-message>\n  <command-args></command-args>"}
        out = {"type": "system", "subtype": "local_command",
               "content": "<local-command-stdout> \x1b[1mContext Usage\x1b[22m\n12k tokens</local-command-stdout>"}
        self.assertEqual(tc.transcript_events(name), [{"t": "user", "text": "/context"}])
        self.assertEqual(tc.transcript_events(out), [{"t": "cmdout", "text": "Context Usage\n12k tokens"}])
        caveat = {"type": "user", "isMeta": True, "message": {"content": "<local-command-caveat>x</local-command-caveat>"}}
        self.assertEqual(tc.transcript_events(caveat), [])
        other = {"type": "system", "subtype": "turn_duration", "content": None}
        self.assertEqual(tc.transcript_events(other), [])

    def test_a_skill_run_by_name_shows_with_its_words(self):
        line = {"type": "user", "message": {"content": "<command-message>grilling</command-message>\n"
                "<command-name>/grilling</command-name>\n<command-args>my plan</command-args>"}}
        self.assertEqual(tc.transcript_events(line), [{"t": "user", "text": "/grilling my plan"}])
        # a skill's own text quotes the tags further in: that is no command
        body = {"type": "user", "message": {"content": "See <command-name>/x</command-name> above"}}
        self.assertEqual([e["text"] for e in tc.transcript_events(body)],
                         ["See <command-name>/x</command-name> above"])

    def write(self, lines):
        path = os.path.join(self.dir, "t.jsonl")
        with open(path, "w") as f:
            for line in lines:
                f.write(json.dumps(line) + "\n")
        return path

    def test_claude_skills_come_from_its_listing(self):
        def listing(names, initial):
            return {"type": "attachment", "attachment": {
                "type": "skill_listing", "isInitial": initial, "names": names,
                "content": "\n".join("- %s: does %s" % (n, n) for n in names)}}
        path = self.write([listing(["old"], True), listing(["tdd", "grilling"], True),
                           listing(["pdf"], False)])
        got = tc.transcript_skills(path, "claude")
        self.assertEqual([(k["name"], k["description"]) for k in got],
                         [("tdd", "does tdd"), ("grilling", "does grilling"), ("pdf", "does pdf")])

    def test_codex_skills_come_from_its_instructions(self):
        text = ("# Using skills\nThe skills are under ### Available skills.\n## Skills\n### Available skills\n"
                "- imagegen: Make images. (file: r1/imagegen/SKILL.md)\n"
                "- chrome-remote-debugging: Drive Chrome: tabs, pages. (file: /h/s/SKILL.md)\n"
                "### Skill roots\n- r1: /home/x\n")
        path = self.write([{"type": "response_item", "payload": {"type": "message", "role": "developer",
                            "content": [{"type": "input_text", "text": text}]}}])
        got = tc.transcript_skills(path, "codex")
        self.assertEqual([(k["name"], k["description"]) for k in got],
                         [("imagegen", "Make images."), ("chrome-remote-debugging", "Drive Chrome: tabs, pages.")])

    def test_the_chat_lists_commands_for_its_ai(self):
        path = self.write([{"type": "attachment", "attachment": {
            "type": "skill_listing", "isInitial": True, "names": ["tdd"], "content": "- tdd: tests first"}}])
        self.mgr.tab_hook = lambda tab, text=None, key=None, queue=False, screen=False: \
            {"title": "x", "status": "idle", "transcript": path, "cli": "claude"}
        code, out = self.mgr.create({"tab": {"id": "t1", "title": "x"}})
        chat = self.mgr.get(out["chat"]["id"])
        chat.mirror_poll()
        code, got = tc.api(self.mgr, "GET", [chat.id, "commands"], {})
        self.assertEqual(code, 200)
        self.assertEqual(got["cli"], "claude")
        self.assertIn("compact", [c["name"] for c in got["builtins"]])
        self.assertNotIn("model", [c["name"] for c in got["builtins"]])     # the chat's own switch
        self.assertEqual(got["skills"], [{"name": "tdd", "description": "tests first"}])

    def test_a_new_conversation_borrows_the_last_ones_skills(self):
        # Claude writes its skill list with the first prompt
        old = self.write([{"type": "attachment", "attachment": {
            "type": "skill_listing", "isInitial": True, "names": ["tdd"], "content": "- tdd: tests first"}}])
        new = os.path.join(self.dir, "new.jsonl")
        with open(new, "w") as f:
            f.write(json.dumps({"type": "system", "subtype": "local_command", "content": "x"}) + "\n")
        os.utime(old, (1, 1))
        self.mgr.tab_hook = lambda tab, text=None, key=None, queue=False, screen=False: \
            {"title": "x", "status": "idle", "transcript": new, "cli": "claude"}
        code, out = self.mgr.create({"tab": {"id": "t1", "title": "x"}})
        chat = self.mgr.get(out["chat"]["id"])
        chat.mirror_poll()
        self.assertEqual([k["name"] for k in chat.commands()["skills"]], ["tdd"])

    def test_a_tab_with_no_file_yet_borrows_too(self):
        self.write([{"type": "attachment", "attachment": {
            "type": "skill_listing", "isInitial": True, "names": ["tdd"], "content": "- tdd: tests first"}}])
        new = os.path.join(self.dir, "not-yet.jsonl")
        self.mgr.tab_hook = lambda tab, text=None, key=None, queue=False, screen=False: \
            {"title": "x", "status": "idle", "transcript": new, "cli": "claude"}
        code, out = self.mgr.create({"tab": {"id": "t1", "title": "x"}})
        chat = self.mgr.get(out["chat"]["id"])
        chat.mirror_poll()
        self.assertEqual([k["name"] for k in chat.commands()["skills"]], ["tdd"])


class HeldWhileScreenTest(Base):
    """A line sent while the tab's Claude shows one of its screens (/usage)
    waits in tabit: typed then, it would close the screen and go in at once."""

    BOX = ["\u2500" * 40, "\u276f\xa0", "\u2500" * 40, "  status line"]
    USAGE = ["  Settings  Status   Config   Usage   Stats", "  Current week", "  Esc to cancel"]
    STATS = ["  Mon \u2592\u2593", "  Active days: 170/193", "    \u2193 stats"]

    def setUp(self):
        super().setUp()
        self.addCleanup(setattr, tc, "MIRROR_EVERY", tc.MIRROR_EVERY)
        tc.MIRROR_EVERY = 3600
        self.path = os.path.join(self.dir, "tx.jsonl")
        open(self.path, "w").close()
        self.screen, self.typed = self.USAGE, []

        def hook(tab, text=None, key=None, queue=False, screen=False):
            if text is None and key is None:
                info = {"title": "x", "status": "ready", "transcript": self.path, "cli": "claude"}
                if screen:
                    info["screen"] = self.screen
                return info
            self.typed.append(text)
            return 200, {"ok": True}
        self.mgr.tab_hook = hook
        code, out = self.mgr.create({"tab": {"id": "t1", "title": "x"}})
        self.chat = self.mgr.get(out["chat"]["id"])

    def test_a_screen_shows_while_the_message_box_is_hidden(self):
        self.assertTrue(tc.screen_dialog_open(self.USAGE))
        self.assertTrue(tc.screen_dialog_open(self.STATS))       # its last line differs
        self.assertFalse(tc.screen_dialog_open(["  some reply"] + self.BOX))
        named = ["\u2500" * 40 + " \u30ec\u30d3\u30e5\u30fc \u2500", "\u276f\xa0", "\u2500" * 40]
        self.assertFalse(tc.screen_dialog_open(named))      # a named conversation's box
        draft = ["\u2500" * 40, "\u276f line 1"] + ["  line %d" % i for i in range(2, 16)] + self.BOX[2:] + ["  a", "  b"]
        self.assertFalse(tc.screen_dialog_open(draft))      # a long draft in the box
        self.assertFalse(tc.screen_dialog_open([]))

    def test_lines_wait_for_the_screen_then_go_in_order(self):
        self.assertEqual(self.chat.mirror_send("one")[1].get("held"), True)
        self.screen = self.BOX       # closed, but "one" still waits: "two" goes behind it
        self.chat.mirror_send("two")
        self.assertEqual(self.typed, [])
        self.assertEqual([q["text"] for q in self.chat.view()["queued"]], ["one", "two"])
        self.chat.mirror_poll()
        self.assertEqual(self.typed, ["one", "two"])
        self.assertEqual(self.chat.view()["queued"], [])

    def test_a_waiting_line_is_taken_back(self):
        self.chat.mirror_send("oops")
        key = self.chat.view()["queued"][0]["key"]
        self.assertEqual(self.chat.unqueue(key), (200, {"ok": True, "text": "oops"}))
        self.screen = self.BOX
        self.chat.mirror_poll()
        self.assertEqual(self.typed, [])

    def test_with_no_screen_open_it_goes_at_once(self):
        self.screen = self.BOX
        self.chat.mirror_send("hi")
        self.assertEqual(self.typed, ["hi"])


class PastedTest(unittest.TestCase):
    """Claude Code saves a paste wrapped in <pasted_content> tags; the chat
    shows the paste without them."""

    def user(self, text):
        return {"type": "user", "message": {"role": "user", "content": text}}

    def test_the_wrapper_goes(self):
        [ev] = tc.transcript_events(self.user(
            '\n\n<pasted_content id="6750">\ncloud team asks:\nHi FW team\n</pasted_content id="6750">\n'))
        self.assertEqual(ev["text"], "cloud team asks:\nHi FW team")

    def test_typed_text_around_it_stays(self):
        [ev] = tc.transcript_events(self.user(
            'look\n\n\n<pasted_content id="54c4">\na\nb\n</pasted_content id="54c4">\n\n\ntest'))
        self.assertEqual(ev["text"], "look\n\n\na\nb\n\n\ntest")

    def test_a_reply_or_a_tab_message_inside_a_paste_reads_as_one(self):
        [ev] = tc.transcript_events(self.user(
            '<pasted_content id="ab12">\n[In reply to Ann: "hi"]\nyes\n</pasted_content id="ab12">'))
        self.assertEqual((ev["text"], ev["reply"]), ("yes", {"who": "Ann", "snip": "hi"}))

    def test_a_tag_that_is_only_written_stays(self):
        # the user writing about the tag is not a paste
        [ev] = tc.transcript_events(self.user('why is <pasted_content id="6750"> there'))
        self.assertEqual(ev["text"], 'why is <pasted_content id="6750"> there')
        bad = '<pasted_content id="aa11">\nx\n</pasted_content id="bb22">'
        [ev] = tc.transcript_events(self.user(bad))
        self.assertEqual(ev["text"], bad)

    def test_a_saved_message_loses_it_on_load(self):
        with tempfile.TemporaryDirectory() as d:
            log = tc._Log()
            log.dir, log.events, log.seq = d, [], 0
            with open(os.path.join(d, "events.jsonl"), "w") as f:
                f.write(json.dumps({"seq": 1, "t": "user",
                                    "text": '<pasted_content id="ab12">\nhi\n</pasted_content id="ab12">'}) + "\n")
                f.write(json.dumps({"seq": 2, "t": "text",
                                    "text": 'see <pasted_content id="ab12">\nx\n</pasted_content id="ab12">'}) + "\n")
            log._load_events()
            self.assertEqual([e["text"] for e in log.events],
                             ["hi", 'see <pasted_content id="ab12">\nx\n</pasted_content id="ab12">'])


if __name__ == "__main__":
    unittest.main()
