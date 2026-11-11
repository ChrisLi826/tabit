#!/usr/bin/env python3
"""A stand-in for `claude -p` in stream-json mode, for tests.

Text it reacts to:  write <name>  asks to use a tool and waits for the answer
                    slow          streams until an interrupt arrives
                    say <text>    answers with exactly <text>
                    later <text>  answers <text> after half a second
                    bounce a b    answers "@a bounce b a": two chats keep it going
                    die           exits at once, with a line on stderr
                    anything else is echoed.
Every start appends its argv to $FAKE_CLAUDE_LOG.
"""
import json
import os
import re
import sys
import time


def out(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def reply(sid, text):
    out({"type": "stream_event", "session_id": sid, "parent_tool_use_id": None,
         "event": {"type": "content_block_delta", "index": 0,
                   "delta": {"type": "text_delta", "text": text[:3]}}})
    out({"type": "assistant", "session_id": sid, "parent_tool_use_id": None,
         "message": {"content": [{"type": "text", "text": text}]}})
    done(sid)


def done(sid):
    out({"type": "result", "subtype": "success", "is_error": False, "session_id": sid,
         "total_cost_usd": 0.001, "duration_ms": 5})


def main():
    argv = sys.argv[1:]
    log = os.environ.get("FAKE_CLAUDE_LOG")
    if log:
        with open(log, "a") as f:
            f.write(json.dumps(argv) + "\n")
    sid = argv[argv.index("--session-id") + 1] if "--session-id" in argv \
        else argv[argv.index("--resume") + 1]
    for line in sys.stdin:
        msg = json.loads(line)
        if msg.get("type") != "user":
            continue
        text = msg["message"]["content"]
        out({"type": "system", "subtype": "init", "session_id": sid})
        body = text.strip().split("\n")[-1].split("]: ", 1)[-1]   # a group adds a briefing line and "[name]: "
        body = re.sub(r"^(@[\w-]+\s*)+", "", body)         # a leading @name is the address
        if body.startswith("bounce "):
            a, b = body.split()[1:3]
            reply(sid, "@%s bounce %s %s" % (a, b, a))
        elif body.startswith("die"):
            sys.stderr.write("boom: it died\n")
            sys.stderr.flush()
            sys.exit(3)
        elif body.startswith("write"):
            out({"type": "assistant", "session_id": sid, "parent_tool_use_id": None,
                 "message": {"content": [{"type": "tool_use", "id": "tu1", "name": "Write",
                                          "input": {"file_path": "/x/" + body[6:]}}]}})
            out({"type": "control_request", "request_id": "rq1",
                 "request": {"subtype": "can_use_tool", "tool_name": "Write",
                             "input": {"file_path": "/x/" + body[6:]},
                             "tool_use_id": "tu1",
                             "permission_suggestions": [{"type": "setMode",
                                                         "mode": "acceptEdits"}]}})
            ans = json.loads(sys.stdin.readline())["response"]["response"]
            reply(sid, "wrote" if ans["behavior"] == "allow" else "denied: " + ans["message"])
        elif body.startswith("slow"):
            for i in range(200):
                out({"type": "stream_event", "session_id": sid, "parent_tool_use_id": None,
                     "event": {"type": "content_block_delta", "index": 0,
                               "delta": {"type": "text_delta", "text": "%d " % i}}})
                time.sleep(0.05)
                # an interrupt arrives on stdin; look without blocking
                import select
                if select.select([sys.stdin], [], [], 0)[0]:
                    req = json.loads(sys.stdin.readline())
                    if req.get("request", {}).get("subtype") == "interrupt":
                        break
            done(sid)
        elif body.startswith("say "):
            reply(sid, body[4:])
        elif body.startswith("later "):
            time.sleep(0.5)
            reply(sid, body[6:])
        else:
            reply(sid, "echo: " + text)


main()
