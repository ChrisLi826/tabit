#!/usr/bin/env python3
"""A phone behind a Tailscale relay is slow per byte: pages, JSON and event
streams go gzipped when the browser takes gzip, each event flushed whole."""
import gzip
import io
import socket
import unittest
import zlib

from test_remote import _ServerCase, tabit


def raw_get(sock_path, path, accept="gzip, deflate, br", until=None):
    s = socket.socket(socket.AF_UNIX)
    s.settimeout(5)
    s.connect(sock_path)
    enc = f"Accept-Encoding: {accept}\r\n" if accept else ""
    s.sendall(f"GET {path} HTTP/1.1\r\nHost: x\r\nTailscale-User-Login: me@example.com\r\n{enc}\r\n".encode())
    data = b""
    while chunk := s.recv(65536):
        data += chunk
        if until and until(data):
            break
    s.close()
    head, _, body = data.partition(b"\r\n\r\n")
    return head.decode(), body


class TestAcceptEncoding(unittest.TestCase):
    def test_reading_the_header(self):
        for accept, want in (("gzip, deflate, br", True), ("br;q=1.0, gzip;q=0.8", True),
                             ("GZIP", True), ("gzip;q=0", False), ("gzip;q=0.0", False),
                             ("deflate, br", False), ("", False), (None, False),
                             ("x-gzip", False), ("gzip;q=junk", False)):
            self.assertEqual(tabit._wants_gzip(accept), want, accept)

    def test_each_write_can_be_read_at_once(self):
        out = io.BytesIO()
        out.close = lambda: None
        z = tabit._GzipStream(out)
        d = zlib.decompressobj(31)
        z.write(b"event: status\ndata: 1\n\n")
        self.assertEqual(d.decompress(out.getvalue()), b"event: status\ndata: 1\n\n")
        seen = len(out.getvalue())
        z.write(b"event: alive\n\n")
        self.assertEqual(d.decompress(out.getvalue()[seen:]), b"event: alive\n\n")
        z.close()
        self.assertEqual(gzip.decompress(out.getvalue()),
                         b"event: status\ndata: 1\n\n" + b"event: alive\n\n")


class TestServerGzip(_ServerCase):
    def setUp(self):
        super().setUp()
        self.app.chat_page = b"<html>" + b"chat " * 2000 + b"</html>"
        self.app._remote_page = lambda name="remote.html": (
            self.app.chat_page if name == tabit.REMOTE_CHAT_PAGE else b"<html>page</html>")

    def test_a_page_is_gzipped_only_when_asked(self):
        head, body = raw_get(self.sock, "/")
        self.assertIn("Content-Encoding: gzip", head)
        self.assertEqual(gzip.decompress(body), self.app.chat_page)
        self.assertLess(len(body), len(self.app.chat_page) // 10)
        head, body = raw_get(self.sock, "/", accept=None)
        self.assertNotIn("Content-Encoding", head)
        self.assertEqual(body, self.app.chat_page)
        head, body = raw_get(self.sock, "/terminal")             # small: as it is
        self.assertNotIn("Content-Encoding", head)

    def test_the_event_stream_is_gzipped_and_arrives_whole(self):
        d = zlib.decompressobj(31)
        got = []

        def first_event(data):
            body = data.partition(b"\r\n\r\n")[2]
            try:
                got[:] = [zlib.decompressobj(31).decompress(body)]
            except zlib.error:
                return False
            return b"\n\n" in got[0]
        head, body = raw_get(self.sock, "/api/events", until=first_event)
        self.assertIn("Content-Encoding: gzip", head)
        self.assertTrue(d.decompress(body).startswith(b"event: status\ndata: "))


if __name__ == "__main__":
    unittest.main()
