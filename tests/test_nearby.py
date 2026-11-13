#!/usr/bin/env python3
"""Nearby: send text and files to another tabit on the LAN, like AirDrop.
Two Nearby objects on this machine stand for two tabits."""
import base64
import json
import os
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import tabit_chat as tc  # noqa: E402


def wait_until(test, secs=5):
    end = time.time() + secs
    while time.time() < end:
        if test():
            return True
        time.sleep(0.02)
    return False


class NearbyTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.me = self.make("me", "me")
        self.you = self.make("you", "you")

    def make(self, name, folder):
        nb = tc.Nearby(port=0, uploads=os.path.join(self.tmp.name, folder), name=name,
                       host="127.0.0.1", publish=False)
        self.addCleanup(nb.turn_off)
        return nb

    def file(self, name, data):
        os.makedirs(self.me.uploads, exist_ok=True)
        path = os.path.join(self.me.uploads, name)
        with open(path, "wb") as f:
            f.write(data)
        return path

    def send_soon(self, text="", paths=()):
        """Send from me to you in the background; gives where the answer lands."""
        got = {}
        t = threading.Thread(target=lambda: got.update(
            res=self.me.send("127.0.0.1", self.you.port, text, list(paths))), daemon=True)
        t.start()
        self.addCleanup(t.join, 10)
        return got, t

    def offer(self):
        self.assertTrue(wait_until(lambda: self.you.status()["offers"]), "no offer came")
        return self.you.status()["offers"][0]

    def test_off_by_default_with_no_server(self):
        self.assertFalse(self.you.status()["on"])
        self.assertIsNone(self.you.srv)
        self.assertEqual(self.me.send("127.0.0.1", 1, "hi", [])[0], 502)    # nobody listens

    def test_an_accepted_offer_lands_in_uploads(self):
        self.you.turn_on(60)
        path = self.file("../../evil name.txt".replace("../", ""), b"hello")
        got, t = self.send_soon("see the log", [path])
        o = self.offer()
        self.assertEqual((o["from"], o["text"], o["files"]), ("me", "see the log", [{"name": "evil name.txt", "size": 5}]))
        code, out = self.you.answer(o["id"], True)
        self.assertEqual(code, 200)
        t.join(10)
        self.assertEqual(got["res"], (200, {"answer": "accepted"}))
        saved = out["files"][0]
        self.assertTrue(saved.startswith(os.path.join(self.you.uploads, time.strftime("%Y-%m-%d"))))
        self.assertEqual(os.path.basename(saved), "evil_name.txt")          # a safe name
        with open(saved, "rb") as f:
            self.assertEqual(f.read(), b"hello")
        self.assertEqual(self.you.status()["got"][0]["text"], "see the log")

    def test_a_declined_offer_leaves_nothing(self):
        self.you.turn_on(60)
        got, t = self.send_soon("", [self.file("a.bin", b"x" * 10)])
        self.you.answer(self.offer()["id"], False)
        t.join(10)
        self.assertEqual(got["res"], (200, {"answer": "declined"}))
        self.assertFalse(os.path.exists(self.you.uploads))
        self.assertEqual(self.you.status()["got"], [])

    def test_turning_off_answers_what_waits_and_stops_listening(self):
        self.you.turn_on(60)
        port = self.you.port
        got, t = self.send_soon("hi")
        self.offer()
        self.you.turn_off()
        t.join(10)
        self.assertEqual(got["res"], (200, {"answer": "off"}))
        self.assertIsNone(self.you.srv)
        self.assertEqual(self.me.send("127.0.0.1", port, "again", [])[0], 502)

    def test_it_turns_itself_off_when_the_time_is_up(self):
        self.you.turn_on(1)
        self.assertTrue(wait_until(lambda: not self.you.on(), 5))

    def test_only_drop_is_served(self):
        self.you.turn_on(60)
        base = "http://127.0.0.1:%d" % self.you.port
        for method, path in (("GET", "/"), ("GET", "/api/chats"), ("POST", "/api/chats/nearby/off"),
                             ("GET", "/drop")):
            req = urllib.request.Request(base + path, data=b"{}" if method == "POST" else None, method=method)
            with self.assertRaises(urllib.error.HTTPError) as e:
                urllib.request.urlopen(req, timeout=5)
            self.assertEqual(e.exception.code, 404, path)

    def test_too_big_is_refused_before_it_is_read(self):
        self.you.turn_on(60)
        req = urllib.request.Request("http://127.0.0.1:%d/drop" % self.you.port, data=b"{}",
                                     headers={"Content-Length": str(tc.NEARBY_MAX * 2)})
        with self.assertRaises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(req, timeout=5)
        self.assertEqual(e.exception.code, 413)

    def test_a_busy_receiver_answers_before_reading(self):
        import socket
        self.you.turn_on(60)
        self.you._last["127.0.0.1"] = time.time()          # it just offered
        with socket.create_connection(("127.0.0.1", self.you.port), timeout=5) as c:
            c.sendall(b"POST /drop HTTP/1.1\r\nHost: x\r\nContent-Length: 20000000\r\n\r\n")
            reply = c.recv(200)                              # no body sent, and still an answer
        self.assertIn(b" 429 ", reply)

    def test_many_connections_at_once_are_not_all_read(self):
        import socket
        self.you.turn_on(60)
        socks = []
        try:
            for i in range(tc.NEARBY_PENDING):     # each holds a slot: headers, no body yet
                c = socket.create_connection(("127.0.0.1", self.you.port), timeout=5)
                c.sendall(b"POST /drop HTTP/1.1\r\nHost: x\r\nContent-Length: 1000\r\n\r\n")
                socks.append(c)
            self.assertTrue(wait_until(lambda: self.you.slots._value == 0))
            self.you._last.clear()
            with socket.create_connection(("127.0.0.1", self.you.port), timeout=5) as c:
                c.sendall(b"POST /drop HTTP/1.1\r\nHost: x\r\nContent-Length: 1000\r\n\r\n")
                self.assertIn(b" 429 ", c.recv(200))   # at once, nothing read
        finally:
            for c in socks:
                c.close()

    def test_connections_at_once_are_capped(self):
        import socket
        self.you.turn_on(60)
        socks = []
        try:
            for _ in range(tc.NEARBY_CONNS):           # open and say nothing
                socks.append(socket.create_connection(("127.0.0.1", self.you.port), timeout=5))
            self.assertTrue(wait_until(lambda: self.you.srv.conns._value == 0))
            with socket.create_connection(("127.0.0.1", self.you.port), timeout=5) as c:
                c.sendall(b"POST /drop HTTP/1.1\r\nHost: x\r\nContent-Length: 2\r\n\r\n{}")
                self.assertEqual(c.recv(100), b"")        # closed at once, no thread for it
        finally:
            for c in socks:
                c.close()
        self.assertTrue(wait_until(lambda: self.you.srv.conns._value == tc.NEARBY_CONNS, 10))

    def test_on_off_on_leaves_no_rule_behind(self):
        rules = []
        def run(*args):
            if args == ("status",):
                return 0, "Status: active\n"
            if args[0] == "allow":
                rules.append(1)
            if args[0] == "delete":
                rules.pop()
            return 0, ""
        self.you.ufw = run
        for _ in range(3):
            self.you.turn_on(60)
            self.you.turn_off()
        self.assertEqual(rules, [])

    def test_off_then_on_at_once_keeps_the_port_open(self):
        # Turn off's slow ufw delete and a new Turn on used to cross: on, no rule
        rules = []
        def run(*args):
            if args == ("status",):
                return 0, "Status: active\n"
            time.sleep(0.3)
            if args[0] == "allow":
                rules.append(1)
            if args[0] == "delete":
                rules.pop()
            return 0, ""
        self.you.ufw = run
        self.you.turn_on(60)
        off = threading.Thread(target=self.you.turn_off)
        off.start()
        time.sleep(0.05)
        self.you.turn_on(60)
        off.join(5)
        self.assertTrue(self.you.on())
        self.assertEqual((rules, self.you.firewall), ([1], "opened"))
        watches = lambda: [t for t in threading.enumerate() if getattr(t, "_target", None)
                           and getattr(t._target, "__name__", "") == "_watch"]
        self.assertTrue(wait_until(lambda: len(watches()) == 1, 3))     # the first one ended

    def test_turning_on_for_longer_beats_the_clock(self):
        self.you.turn_on(60)
        self.you.until = time.time() - 1          # the time was up ...
        self.you.until = time.time() + 600        # ... and On for 10 minutes came just then
        self.you.turn_off(expired=True)
        self.assertTrue(self.you.on())

    def test_a_failed_save_tells_the_sender_at_once(self):
        self.you.turn_on(60)
        got, t = self.send_soon("", [self.file("a.bin", b"x")])
        o = self.offer()
        with mock.patch.object(tc, "save_upload", side_effect=OSError("disk full")):
            self.assertEqual(self.you.answer(o["id"], True)[0], 500)
        t.join(5)
        self.assertEqual(got["res"], (200, {"answer": "failed"}))

    def test_a_junk_port_is_refused(self):
        mgr = type("M", (), {"nearby": self.me})()
        self.assertEqual(tc.nearby_api(mgr, "POST", ["send"], {"to": {"address": "10.0.0.1", "port": "x"}, "text": "hi"})[0], 400)

    def test_waiting_offers_are_capped(self):
        self.you.turn_on(60)
        body = {"from": "x", "text": "hi"}
        threads = []
        for i in range(tc.NEARBY_PENDING):
            t = threading.Thread(target=self.you.offer, args=("10.0.0.%d" % i, body), daemon=True)
            t.start()
            threads.append(t)
        self.assertTrue(wait_until(lambda: len(self.you.status()["offers"]) == tc.NEARBY_PENDING))
        self.assertEqual(self.you.offer("10.0.0.99", body)[0], 429)
        self.you.turn_off()
        for t in threads:
            t.join(5)

    def test_one_address_waits_between_offers(self):
        self.you.turn_on(60)
        with mock.patch.object(tc, "NEARBY_WAIT", 0):
            self.you.offer("10.0.0.1", {"text": "one"})
        self.assertEqual(self.you.offer("10.0.0.1", {"text": "two"})[0], 429)

    def test_only_uploaded_files_go_out(self):
        self.assertEqual(self.me.send("127.0.0.1", 1, "", ["/etc/passwd"])[0], 400)
        self.assertEqual(self.me.send("8.8.8.8", 1, "hi", [])[0], 400)       # not the LAN

    def test_peers_from_avahi(self):
        out = "\n".join([
            "+;enp4s0;IPv4;ann@pc;_tabit-drop._tcp;local",
            '=;docker0;IPv4;ann@pc;_tabit-drop._tcp;local;pc.local;172.17.0.1;47820;"id=aaa"',
            '=;enp4s0;IPv4;ann@pc;_tabit-drop._tcp;local;pc.local;10.0.217.40;47820;"id=aaa"',
            '=;enp0s31f6;IPv4;ann@pc;_tabit-drop._tcp;local;pc.local;192.168.10.9;47820;"id=aaa"',
            '=;enp4s0;IPv6;ann@pc;_tabit-drop._tcp;local;pc.local;fe80::1;47820;"id=aaa"',
            '=;enp4s0;IPv4;me\\059x;_tabit-drop._tcp;local;me.local;10.0.217.35;47820;"id=%s"' % self.me.id,
            '=;enp4s0;IPv4;bob\\064lab;_tabit-drop._tcp;local;lab.local;10.0.217.41;47821;"id=bbb"'])
        self.me.browse = lambda: out
        self.assertEqual(self.me.peers(), [
            {"name": "ann@pc", "address": "10.0.217.40", "port": 47820, "id": "aaa"},
            {"name": "bob@lab", "address": "10.0.217.41", "port": 47821, "id": "bbb"}])

    def test_a_chinese_name_reads_as_it_was(self):
        # what avahi-browse really wrote for "測試@電腦"
        line = ('=;enp4s0;IPv4;\\230\\184\\172\\232\\169\\166\\064\\233\\155\\187\\232\\133\\166;'
                '_tabit-drop._tcp;local;pc.local;10.0.217.40;47899;"id=zz9"')
        self.assertEqual(tc.nearby_parse_browse(line)[0]["name"], "測試@電腦")

    def test_a_v6_rule_left_alone_is_found_too(self):
        self.you.port = 47820
        self.you.ufw = lambda *a: (0, "Status: active\n\n47820/tcp (v6)  ALLOW  Anywhere (v6)  # tabit nearby\n")
        self.assertEqual(self.you._rule(), (True, "47820/tcp (v6)  ALLOW  Anywhere (v6)  # tabit nearby"))

    def test_the_publisher_stops_when_its_pipe_closes(self):
        nb = tc.Nearby(port=0, uploads=self.tmp.name, name="t", host="127.0.0.1", publish=False)
        self.addCleanup(nb.turn_off)
        with mock.patch.object(tc.subprocess, "Popen", wraps=tc.subprocess.Popen) as popen:
            nb.publish = True
            with mock.patch.dict(os.environ, {"PATH": self.tmp.name + ":" + os.environ["PATH"]}):
                fake = os.path.join(self.tmp.name, "avahi-publish")
                with open(fake, "w") as f:
                    f.write("#!/bin/sh\nexec sleep 30\n")
                os.chmod(fake, 0o755)
                nb.turn_on(60)
                proc = nb._pub[0]
                argv = popen.call_args.args[0]
        self.assertEqual(argv[3:], ["t", tc.NEARBY_TYPE, str(nb.port), nb.id])
        self.assertTrue(wait_until(lambda: tc.subprocess.run(
            ["pgrep", "-f", "^sleep 30$"], capture_output=True).returncode == 0))
        nb.turn_off()
        self.assertIsNotNone(proc.poll())        # the wrapper ended with the pipe
        self.assertTrue(wait_until(lambda: tc.subprocess.run(
            ["pgrep", "-f", "^sleep 30$"], capture_output=True).returncode != 0))   # and took avahi-publish along

    def test_an_accepted_offer_can_go_on_to_a_chat(self):
        self.you.turn_on(60)
        got, t = self.send_soon("please check", [self.file("log.txt", b"x")])
        o = self.offer()
        mgr = type("M", (), {"nearby": self.you, "get": lambda m, cid: object() if cid == "c1" else None})()
        self.assertEqual(tc.nearby_api(mgr, "POST", ["answer"], {"id": o["id"], "accept": True, "chat": "nope"})[0], 404)
        calls = []
        real = tc.api
        with mock.patch.object(tc, "api", lambda m, method, parts, body: calls.append((parts, body)) or (200, {"ok": True})):
            code, out = tc.nearby_api(mgr, "POST", ["answer"], {"id": o["id"], "accept": True, "chat": "c1"})
        self.assertIs(tc.api, real)
        self.assertEqual((code, out["sent"]), (200, True))
        parts, body = calls[0]
        self.assertEqual(parts, ["c1", "send"])
        self.assertEqual(body["text"], "📡 From me (Nearby):\nplease check\n\nFiles:\n" + out["files"][0])
        t.join(10)

    def test_the_message_fits_one_send(self):
        msg = tc.nearby_message("ann@pc", "x" * (tc.MAX_SEND + 50), ["/a/b.txt"])
        self.assertLessEqual(len(msg), tc.MAX_SEND)
        self.assertTrue(msg.endswith("Files:\n/a/b.txt"))
        many = ["/" + "p" * 2000] * 11                      # paths alone over the limit: no text, not its tail
        self.assertNotIn("x", tc.nearby_message("ann@pc", "x" * 50, many))
        self.assertEqual(tc.nearby_message("ann@pc", "", ["/a"]), "📡 From ann@pc (Nearby):\n\nFiles:\n/a")

    def ufw_fake(self, status):
        calls = []
        def run(*args):
            calls.append(args)
            if args == ("status",):
                return status
            return 0, ""
        return calls, run

    def test_the_firewall_opens_while_receiving_and_closes_after(self):
        calls, run = self.ufw_fake((0, "Status: active\n\nTo  Action  From\n22/tcp  ALLOW  Anywhere\n"))
        self.you.ufw = run
        self.you.turn_on(60)
        self.assertEqual(self.you.status()["firewall"], "opened")
        self.assertIn(("allow", "%d/tcp" % self.you.port, "comment", tc.NEARBY_RULE), calls)
        self.you.turn_off()
        self.assertEqual(calls[-1], ("delete", "allow", "%d/tcp" % self.you.port))

    def test_a_rule_of_yours_is_left_alone(self):
        calls, run = self.ufw_fake((0, ""))
        self.you.ufw = run
        self.you._rule = lambda: (True, "47820/tcp  ALLOW  Anywhere")    # no comment: not tabit's
        self.you.turn_on(60)
        self.assertEqual(self.you.firewall, "already")
        self.you.turn_off()
        self.assertEqual([c for c in calls if c[0] in ("allow", "delete")], [])

    def test_ufw_answers_in_english_through_sudo(self):
        # a Chinese desktop got "狀態：不活動" and read an active firewall as off
        done = mock.Mock(returncode=0, stdout="Status: active\n", stderr="")
        with mock.patch.object(tc.subprocess, "run", return_value=done) as run:
            self.assertEqual(tc.ufw("status"), (0, "Status: active\n"))
        self.assertEqual(run.call_args.args[0],
                         ["sudo", "-n", "env", "LANGUAGE=C", "LC_ALL=C", "LANG=C", "ufw", "status"])
        asks = mock.Mock(returncode=1, stdout="", stderr="sudo: a password is required\n")
        with mock.patch.object(tc.subprocess, "run", return_value=asks):
            self.assertEqual(tc.ufw("status"), (None, ""))

    def test_off_or_no_sudo(self):
        self.you.ufw = lambda *a: (0, "Status: inactive\n")
        self.assertEqual(self.you._open_port(), "none")
        self.you.ufw = lambda *a: (None, "")
        self.assertEqual(self.you._open_port(), "manual")

    def test_a_rule_left_by_a_dead_tabit_is_taken_away(self):
        self.you.port = 47820
        calls, run = self.ufw_fake((0, "Status: active\n\n47820/tcp  ALLOW  Anywhere  # tabit nearby\n"))
        self.you.ufw = run
        self.you.clean_left_rule()
        self.assertEqual(calls[-1], ("delete", "allow", "47820/tcp"))

    def test_the_page_api(self):
        mgr = type("M", (), {"nearby": self.you})()
        self.assertEqual(tc.api(mgr, "POST", ["nearby", "on"], {"for": "forever"})[0], 400)
        code, out = tc.api(mgr, "POST", ["nearby", "on"], {"for": "10m"})
        self.assertEqual((code, out["on"]), (200, True))
        self.assertAlmostEqual(out["until"], time.time() + 600, delta=5)
        self.assertEqual(tc.api(mgr, "POST", ["nearby", "answer"], {"id": "nope", "accept": True})[0], 404)
        code, out = tc.api(mgr, "POST", ["nearby", "off"], {})
        self.assertEqual((code, out["on"]), (200, False))


if __name__ == "__main__":
    unittest.main()
