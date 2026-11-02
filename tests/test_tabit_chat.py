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
import time
import unittest

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
    def test_main_chat_is_made_once_listed_first_and_not_deletable(self):
        self.mgr.ensure_default()
        mine = self.new(title="Later")
        self.mgr.ensure_default()
        rows = self.mgr.listing()
        self.assertEqual([r["default"] for r in rows], [True, False])
        self.assertEqual(rows[0]["title"], tc.DEFAULT_TITLE)
        main = self.mgr.get(rows[0]["id"])
        code, out = tc.api(self.mgr, "POST", [main.id, "delete"], {})
        self.assertEqual(code, 400)
        self.assertIn(main.id, self.mgr.chats)
        self.assertEqual(tc.api(self.mgr, "POST", [mine.id, "delete"], {})[0], 200)

    def test_main_chat_survives_a_restart_and_has_its_role(self):
        self.mgr.ensure_default()
        main = next(c for c in self.mgr.chats.values() if c.meta.get("default"))
        main.send("hello")
        self.assertTrue(wait(lambda: any(e["t"] == "done" for e in main.events)))
        with open(self.log) as f:
            self.assertIn("main assistant", f.read())
        self.stop_all()
        again = self.make()
        again.ensure_default()
        self.assertEqual(sum(1 for c in again.chats.values() if c.meta.get("default")), 1)


class AccountTest(Base):
    def make(self):
        return tc.ChatManager(root=os.path.join(self.dir, "chats"), claude=FAKE,
                              env_drop=("CLAUDECODE",),
                              accounts=lambda: [("Work", FAKE)],
                              auth=lambda cli: {"loggedIn": True, "email": "a@b.c", "subscriptionType": "pro"})

    def test_one_login_for_every_chat_and_it_can_change(self):
        chat = self.new()
        rows = self.mgr.account_choices()
        self.assertEqual([r["name"] for r in rows], ["", "Work"])
        self.assertEqual(rows[1]["label"], "Work — a@b.c · pro")
        self.assertEqual(tc.api(self.mgr, "POST", ["account"], {"account": "nobody"})[0], 400)
        self.assertEqual(tc.api(self.mgr, "POST", ["account"], {"account": "Work"})[0], 200)
        self.assertEqual(chat.summary()["account"], "Work")
        self.assertEqual(self.make().account, "Work")           # it is remembered

    def test_the_login_cannot_change_while_a_chat_works(self):
        chat = self.new()
        chat.send("slow")
        self.assertTrue(wait(lambda: chat.state != "idle"))
        self.assertEqual(tc.api(self.mgr, "POST", ["account"], {"account": "Work"})[0], 409)
        chat.interrupt()


class AccountMirrorTest(AccountTest):

    def test_a_mirrored_tab_at_work_does_not_hold_the_login(self):
        self.mgr.tab_hook = lambda tab, text=None, key=None, queue=False: \
            {"title": "MOT", "status": "working", "transcript": "", "claude": True}
        code, out = self.mgr.create({"tab": {"id": "t1", "title": "x"}})
        mirror = self.mgr.get(out["chat"]["id"])
        mirror.tab_state = "working"            # its tab works; this chat runs nothing
        self.assertEqual(tc.api(self.mgr, "POST", ["account"], {"account": "Work"})[0], 200)


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
        self.assertEqual(later["events"][0]["text"], "two")

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

    def test_a_reply_can_name_several_chats(self):
        group, (a, b, c) = self.group(3)
        self.assertEqual([x.id for x in group._mentions("@bob, @cy are you there", skip=a)], [b.id, c.id])

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

    def test_a_chat_can_pass_a_message_to_another(self):
        group, (a, b) = self.group()
        # bob is told to say a line that starts with @alice: it reaches alice
        group.send("@bob say @alice are you there")
        self.assertTrue(wait(lambda: any(e["t"] == "relay" for e in group.events)))
        self.assertTrue(wait(lambda: any(n == "alice" for n, _ in self.says(group))))
        relay = [e for e in group.events if e["t"] == "relay"][0]
        self.assertEqual((relay["from"], relay["to"]), ("bob", "alice"))
        got = [e["text"] for e in a.events if e["t"] == "user"][0]
        self.assertIn("[bob]: @alice are you there", got)
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

        def hook(tab, text=None, key=None, queue=False):
            if tab != "t1":
                return (404, {"error": "no"}) if text or key else {"error": "no"}
            if text is None and key is None:
                return {"title": "MOT", "status": self.status,
                        "transcript": self.path, "claude": True}
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
                          ("tool", "Bash"), ("result", "a.txt"), ("text", "Fixed.")])

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
        tc.api(self.mgr, "POST", [chat.id, "stop"], {})
        self.assertEqual(self.calls[-1], (None, "esc", False))

    def test_only_a_claude_tab_and_not_in_a_group(self):
        old = self.mgr.tab_hook
        self.mgr.tab_hook = lambda tab, *a, **k: dict(old(tab), claude=False)
        self.assertEqual(self.mgr.create({"tab": {"id": "t1"}})[0], 400)
        self.mgr.tab_hook = old
        self.assertEqual(self.mgr.create({"tab": {"id": "zz"}})[0], 400)
        self.assertEqual(self.mgr.create({"tab": {"id": "a b;rm"}})[0], 400)
        mirror, plain = self.mirror(), self.new()
        code, out = self.mgr.create_group({"members": [mirror.id, plain.id]})
        self.assertEqual(code, 400)



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


if __name__ == "__main__":
    unittest.main()
