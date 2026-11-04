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

    def test_several_at_names_with_commas_talk_to_those_only(self):
        group, (a, b, c) = self.group(3)
        group.send("@alice, @cy, say just us")
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

    def test_only_a_claude_tab(self):
        old = self.mgr.tab_hook
        self.mgr.tab_hook = lambda tab, *a, **k: dict(old(tab), claude=False)
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
            f.write(json.dumps({"type": "assistant", "message": {"content": [
                {"type": "text", "text": "@bob can you check it"}]}}) + "\n")
        chat.mirror_poll()
        self.assertIn(("mot", "@bob can you check it"),
                      [(e["name"], e["text"]) for e in group.events if e["t"] == "say"])
        self.assertTrue(wait(lambda: any(e["t"] == "relay" and e["to"] == "bob" for e in group.events)))
        self.assertTrue(wait(lambda: any("[mot]: @bob can you check it" in e["text"]
                                         for e in bob.events if e["t"] == "user")))

    def test_old_lines_of_a_mirror_are_not_passed_on(self):
        # the first look reads the tab's past: it is history, not news
        with open(self.path, "a") as f:
            f.write(json.dumps({"type": "assistant", "message": {"content": [
                {"type": "text", "text": "@bob an old line"}]}}) + "\n")
        chat, bob = self.mirror(), self.new(title="Bob")
        code, out = self.mgr.create_group({"members": [chat.id, bob.id]})
        group = self.mgr.groups[out["group"]["id"]]
        chat.mirror_poll()
        self.assertEqual([e for e in group.events if e["t"] in ("say", "relay")], [])
        self.assertFalse(any(e["t"] == "user" for e in bob.events))



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
        self.mgr.tab_hook = lambda tab, text=None, key=None, queue=False: {
            "title": "MOT", "status": "idle", "transcript": "", "claude": True}
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

    def test_no_answer_or_no_tabs_changes_nothing(self):
        self.mgr.sync_tabs()
        for gone in (None, []):
            self.tabs = gone
            for _ in range(tc.GONE_AFTER + 1):
                self.mgr.sync_tabs()
        self.assertEqual(self.titles(), ["Blog", "MOT"])

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


class TabSwitchTest(Base):
    """The login and the model of a chat, from its page."""

    def setUp(self):
        super().setUp()
        self.addCleanup(setattr, tc, "MIRROR_EVERY", tc.MIRROR_EVERY)
        tc.MIRROR_EVERY = 3600
        self.typed, self.ops, self.busy = [], [], False
        self.path = os.path.join(self.dir, "tx.jsonl")
        self.mgr.accounts = lambda: [("Personal", FAKE)]

        def hook(tab, text=None, key=None, queue=False):
            if text is None and key is None:
                return {"title": "MOT", "status": "idle", "transcript": self.path, "claude": True}
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

        def hook(tab, text=None, key=None, queue=False):
            if text is None and key is None:
                info = {"title": "MOT", "status": self.status, "transcript": "", "claude": True}
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

        def hook(tab, text=None, key=None, queue=False):
            if text is None and key is None:
                return {"title": self.titles[tab], "status": "idle", "transcript": self.tx[tab], "claude": True}
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
        self.say("ta", "Let me ask.\n@beta can you check the build?")
        self.assertEqual(self.typed, [("tb", "[from @alpha]: Let me ask.\n@beta can you check the build?")])
        self.assertTrue(any(e["t"] == "relay" and e["to"] == "beta" for e in self.a.events))
        self.say("tb", "@alpha it passes")                   # and back
        self.assertEqual(self.typed[-1], ("ta", "[from @beta]: @alpha it passes"))

    def test_no_mention_no_name_and_no_self_send_nothing(self):
        self.say("ta", "all done, nobody to ask")
        self.say("ta", "@nobody hello")
        self.say("ta", "@alpha talking to myself")
        self.assertEqual(self.typed, [])

    def test_the_past_is_not_sent(self):
        with open(self.tx["ta"], "a") as f:
            f.write(json.dumps({"type": "assistant", "message": {"content": [
                {"type": "text", "text": "@beta an old line"}]}}) + "\n")
        self.a.meta["mirror"] = {}                            # a first look again: the past
        self.a.mirror_poll()
        self.assertEqual(self.typed, [])

    def test_a_tab_that_cannot_take_it_says_so_in_the_sender(self):
        self.refuse.add("tb")
        self.say("ta", "@beta hello")
        self.assertTrue(any(e["t"] == "error" and "beta" in e["text"] for e in self.a.events))

    def test_tabs_in_one_group_leave_it_to_the_group(self):
        code, out = self.mgr.create_group({"members": [self.a.id, self.b.id]})
        self.assertEqual(code, 200, out)
        self.say("ta", "@beta hello")
        self.assertEqual(len([t for t in self.typed if t[0] == "tb"]), 1)

    def test_tabs_of_one_name_are_told_apart_by_their_group(self):
        for c, group in ((self.a, "EHR330"), (self.b, "Work")):
            c.meta["title"] = "claude"
            c.meta["tab"]["group"] = group
        names = sorted(self.mgr.tab_names())
        self.assertEqual(names, ["ehr330-claude", "work-claude"])

    def send(self, chat, text):
        return tc.api(self.mgr, "POST", [chat.id, "send"], {"text": text})

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


if __name__ == "__main__":
    unittest.main()
