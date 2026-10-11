# Telegram

Pushes and tasks through a Telegram bot. Back to the [README](../README.md).

The same news can also reach your **phone**. **Settings… → Telegram →
Also send it to Telegram** takes a bot token (from `@BotFather`) and
sends one message per agent that finishes or wants you. **How do I set
this up?** on that page has the five steps, and **Find it** next to Chat
id reads your own id off the first message you send the bot, so there is
no second bot to go and ask. By default it only sends while tabit is not the window in
front; **Always** sends either way. It is deliberately not tied to the
desktop popup switch, because "no popup on this screen" and "nothing on
my phone" are two different wishes. **Send a test message** proves the
pair before you rely on it. The token is kept in
`~/.config/tabit/telegram.json`, mode `0600` — not in `settings.json`,
which is the file you would paste into a bug report.

Two more switches sit under it. **Answer /status from the phone** makes
tabit read what you send the bot and reply to `/status` with every AI tab
and what it is doing; only your own 1:1 chat is listened to. /status types
nothing into a tab; a task does that (below). **Include the agent's last lines in the
message** puts the agent's own words in the push, because otherwise a
message says an agent wants you but not what it asked — and you cannot
answer from a phone without that. It is off by default: those lines
travel through Telegram, and a console can have secrets on it.

**Sending a task back.** Right-click an AI tab → **Telegram tasks…** and
give it a short name. That name is the switch: a tab without one is not
offered on the phone and is refused if anything tries. With one, write
the task in Telegram, send it, and tap the tab to send it to — the tap
is the submit, and a plain message never goes anywhere on its own.
**Replying** to one of tabit's messages skips the tap, because the reply
already says which tab. Putting a tab's short name on the first line
narrows the buttons to that one tab. The task arrives the way a paste
does, so a prompt with line breaks in it is not submitted a line at a
time, and exactly one Enter goes on the end. A button works once:
pressing it again says so rather than running the task twice.

**Getting the answer back.** A task sent from Telegram carries a short
footer telling the agent where to write its reply and to keep it to
about ten lines with the result first — it is going to a phone, and an
agent given no budget writes a report. tabit sends that file back as a
reply to the task — so one task reads as one thread. The
file is the good path: the last lines of a terminal are the last lines
of a redraw, not an answer to anything. If the agent stops without
writing one, tabit says so and falls back to the tail of the screen.
Either way it comes back whatever **Send** is set to: "not in front"
means this window lost the keyboard focus, which is not the same as you
being away, and an answer you asked for from your phone should not
depend on it. Turn the extra line off with **A task asks the agent to
write its answer to a file** if you would rather tabit never touched
your prompt.
