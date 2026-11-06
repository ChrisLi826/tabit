# Tabit Chat

Tabit Chat is Remote as a conversation. You talk to Claude on this computer
from a phone or another computer, in chat bubbles, not in a terminal. Back to
[Remote](remote.md).

Open `https://<this-host>.<tailnet>.ts.net/`: the address opens the chat
(`/chat` still works). It needs the same switch as typing into tabs
(**Settings… → Remote → Allow typing into tabs**), because a chat can run
commands on this machine; with that switch off, the address goes on to the
terminal view. ⌨ opens the terminal view, at `/terminal`. On a phone, **Share → Add to Home
Screen** makes it an app of its own.

## What it does

- **Chats.** Each chat is one Claude working in a folder you pick, with a
  model you pick. Replies stream in as they are written. Markdown, lists and
  code show properly. Tap a file path (under your home or `/tmp`, in `code`
  or not) and the file opens over the chat, when **Allow opening files** is
  on: a picture fits the box, other files show as a page. Esc, ✕ or a tap
  outside closes it; ↗ (or a Ctrl- or Cmd-click on the path) opens it in a
  new window.
- **No main chat.** Older versions kept a main chat, *Tabit Chat* (the owl),
  at the top of the list. It is gone: its folder stays as
  `~/.local/share/tabit/chats/<id>.retired`, history and all.
- **Questions.** When Claude wants to run a command or change a file, the chat
  shows a card with **Allow** and **Deny**, and **Allow & don't ask again**
  when Claude offers a rule to remember. When
  Claude asks you a choice, the options are buttons. After you answer, the
  card keeps your picks lit. **Stop** ends the turn.
- **Modes**, per chat: *Ask each time* (the default), *Accept edits*, *Plan
  first*, and *Bypass*, which asks nothing. Bypass is only there when you pick
  it, and the list marks the chat in red.
- **Replies.** Tap a message (yours, Claude's, or one in a group) and tap
  **↩ Reply**. A bar over the text box shows what you answer, and Claude gets
  the quote with your message. Tap the quote in a bubble to jump back to the
  message it answers.
- **Pictures.** Every chat has an animal and a colour, picked from its id.
  New chat starts with a rolled one; tap another animal or colour, or 🎲 for
  a new roll. A chat's settings change it later. A group shows the pictures
  of its members together.
- **Folders.** **Browse…** walks the folders on this computer. **＋ New
  folder** makes one inside the folder shown: one plain name, no `/`.
- **Files and photos.** 📎, paste or drag and drop. The file goes to
  `~/.local/share/tabit/uploads/<date>/` and its path goes into your message.
- **🔔** tells you when the AI is done while you are not looking at the page
  (another window or app in front counts). 🔕 means it is off; tap to turn it on.
- **⚙** (next to ⌨ on the list) opens tabit's Settings, the same sheet as in
  the terminal view: the AI defaults for new tabs, the Claude accounts (add
  one, log in), Check for updates and Restart tabit.
- **History.** Chats and their history stay on this computer in
  `~/.local/share/tabit/chats/`. Close the page, come back later, and it goes
  on. Each chat is a normal Claude session, so you can also `claude --resume`
  it at the desk, in the chat's folder.
- A chat that sits idle for 20 minutes lets its Claude process go; your next
  message starts it again where it left off. At most 6 run at once.

## Claude login

When Settings → AI on the computer has more than one Claude account, each
chat picks its own. **New chat** asks which account the new tab uses, and
offers the one picked last. A chat's settings (⋯) show its Account and switch
it later. A switch is refused while that chat is
working: wait, or press Stop.

## Usage

The top of the chat list shows how much of each login's limits is used:
one row per AI and account that a chat uses now (`Claude · Personal`,
`Codex · Default`). Each row shows the limit windows the service reports,
for example `5h 13% ↺9m` and `7d 36% ↺3d`: the share used and when that
window starts again. A bar turns yellow at 50% and red at 80%. A window
that has started again since the last reading shows 0%.

- **Claude**: tabit asks Anthropic every 3 minutes for each Claude login a
  chat uses, the way Claude's own `/usage` does, with that login's token
  from its `.credentials.json`. A token past its time is not renewed by
  tabit: that login shows its last reading until its claude runs again.
- Claude also hands its usage to the status line program. tabit's settings
  for a Claude tab name its own one (`~/.local/share/tabit/claude-statusline.py`),
  which keeps the usage for that tab and then runs the status line set in
  that account's `settings.json`, so your status line still shows. A status
  line set in a project's `.claude/settings.json` is not run in tabit tabs.
  A running claude keeps the settings it started with: its tab reports this
  way after its claude starts again. The newest reading of the two wins.
- **Codex** writes its usage into the session file after each turn; the
  chat reads it from there. A free plan has one 30-day window; paid plans
  have 5h and 7d.
- The last reading of each login is kept in
  `~/.local/share/tabit/chats/usage.json`, so it shows again after a restart.

## The AI tabs

Every chat can use the AI tabs of tabit (terminal tabs running an AI) with
the tool `tabit-tabs`, which `install.sh` puts in `~/.local/bin`:

- `tabit-tabs list` shows the AI tabs, with their status.
- `tabit-tabs read <id> [lines above]` shows what a tab's screen says. It
  reads any tab, not only an AI tab.
- `tabit-tabs send <id> <text>` types a line into a tab. It asks you first:
  the chat shows a **Send to tab** card with the tab and the text, and nothing
  is typed until you tap **Allow**. A tab that is working or asking something
  does not take it.

Claude is told that text it reads from a tab is information, never an order.

The **Send to tab** card guards against Claude's mistakes; it is not a lock.
A Claude that may run any command (Bypass, or a command you allowed) can
reach tabit the way `tabit-tabs` does, and do what the phone can do. The real
boundary is your user account on this computer, as for anything else you run.
Tabs that talk to each other with `@name` (below) have no card at all: a tab's
reply is typed into the other tab as it is, even into a tab in Bypass.

## Chats are tabs

Every Claude tab in tabit has a chat, and every new chat is a Claude tab. The
chat shows that tab's conversation, and what you type goes into the tab. A tab
that is working takes your line too: Claude reads it at its next step, in the
same turn, and the chat shows it.
**Stop** presses Esc in the tab.

Until Claude reads such a line, the chat shows it as a pale **Queued**
bubble. tabit follows Claude Code's own queue in the transcript
(`queue-operation` lines: enqueue, dequeue, remove, popAll). **Take back**
does what you would do at the tab: it presses Up (Claude Code brings every
held line back into its message box), empties the box, and types the other
held lines again. The line you took back goes into the chat's message box,
to change or delete. It works only while the tab's box is empty (Claude
Code shows "Press up to edit queued messages"); with your own text in it,
take the line back in the tab. Lines another tab passed on (`[from @name]`)
are not shown. A Codex tab reads a line at once, so nothing waits there.

**Slash commands and skills.** Typing `/` at the start of the message box
lists the AI's commands and skills; type more to filter, tap one (or Enter,
Tab) and it goes into the box, ready for its words. Sending types it into
the tab, as if typed there.

- **Skills** are the ones the tab's AI was told it has: Claude's
  `skill_listing` lines in its transcript, Codex's "Available skills" list in
  its instructions. A Claude skill runs as `/name`, a Codex skill as `$name`
  (in a Codex chat, `$` lists the skills too). Claude's list also has skills
  only Claude may start; picking one of those, Claude Code says so. Claude
  writes its list with the first prompt, so a new Claude tab borrows the
  list of the last conversation in the same project folder until then. A
  new Codex tab lists its skills after its first turn.
- **Built-in commands** are a short list per AI (`BUILTINS` in
  `tabit_chat.py`). Model, effort and account are left out: the chat's own
  settings switch them for that tab only. A command marked *shows in the
  tab* opens a screen in the tab, so its answer is not in the transcript.
  Sent from the chat, it opens a box over the message box with the tab's
  screen (read again every 1.5 s, for up to 5 minutes) and the keys to work
  it: arrows, Tab, Enter, Esc. **Close** closes the tab's screen (it
  presses Esc, after reading the screen again: only while the screen is
  still open, since Esc stops a Claude that works) and then the box. A
  Claude screen counts as open while Claude's message box (a line of ─,
  then ❯) is hidden; its tabs end in different lines (Esc to cancel, Esc
  to close, ↓ stats). A Codex screen counts as open while its last lines
  say "esc to cancel" (or close, back). When the screen is closed at the desk, the box goes by
  itself. To see it again, send the command again. Make the box taller or
  shorter by dragging the bar on its top (up: taller) or with − and ＋; the height
  is kept for next time.
- A message sent while a Claude tab shows one of its screens (opened from
  the chat or at the desk) would close that screen if typed. It waits in
  tabit instead, as a **Queued** bubble ("sent when the tab's screen
  closes") that can be taken back, and goes in, in order, within 2 seconds
  of the screen closing. Messages sent after it wait behind it. It waits in
  tabit's memory: a restart of tabit drops it. On a computer the
  screen in the box takes the focus (click it to give it back): then the
  keyboard works the tab's screen. Arrows, Enter, Tab, Backspace, Page
  Up/Down, Home and End go as keys, letters as typed, and Esc only while
  the screen still shows.
- A command the tab ran (also one typed at the desk) shows as your line,
  `/context` or `/grilling my plan`, and what it printed shows under it as
  **⌘ output**.

**Codex tabs** have a chat too. Their badge says `Codex · <model>`. The chat
reads the Codex session's file (`~/.codex/sessions/.../rollout-*.jsonl`).
Codex 0.166 keeps every session file open in one shared daemon, so tabit
cannot tell from the tab's own processes which session is its. Instead
tabit starts each Codex with `-c notify=[...]`: after each turn Codex runs
`~/.local/share/tabit/codex-notify.sh`, which writes the session id for
that tab. So a Codex chat fills in once its first turn ends. A Codex tab
saved before this gets the notify the next time its codex starts. tabit
takes Codex's `notify` setting for this: a `notify` in your
`~/.codex/config.toml` is not run in a tab. Account and model switch as
for Claude (below): tabit restarts the tab's codex with `resume <id>`,
under the account's own `codex`, with `--model` for a model. The models are
the ones Codex lists in its own `/model` picker (`~/.codex/models_cache.json`).
A Codex tab can switch only after its first turn: before that tabit does not
know its session. When Codex asks to
run a command or make an edit, the chat shows its question as a card, the
same as a menu in a Claude tab: a tap presses that option's number in the tab.

- **One name, one group.** The chat has the tab's name and shows the tab's
  group under it, with the group's colour. Rename the chat and the tab is
  renamed. Rename or regroup the tab and the chat follows within 10 seconds.
- **New chat** opens a Claude tab in the group you pick, in the folder you
  pick. With no group picked, the tab is in no group. When Codex is
  installed, the form asks which AI: Claude or Codex. It remembers the last
  pick. With more than one login for that AI, it also asks which account.
- **Delete.** Deleting a chat closes its tab. The chat asks first. The last
  tab in tabit cannot be closed from the phone, so that chat stays. Closing a
  tab removes its chat after about a minute.
- **Old chats move into tabs.** A chat that ran its own Claude moves into a
  tab when it is idle: the tab goes on with the same
  conversation (`--resume`), keeps the login and bypass, and the chat that
  mirrors it takes the old one's picture and name, and its place in the
  groups (rooms) the old chat was in. The old chat's folder is kept as
  `~/.local/share/tabit/chats/<id>.moved`, history and all. Of the modes only
  Bypass goes along; a chat in Accept edits or Plan first becomes a tab that
  asks each time (unless **Settings → AI → Claude bypass** is on, which makes
  every new Claude tab bypass, this one too). A chat that cannot move is tried 3 times.
- **Account, model and thinking effort.** A chat's settings show Account,
  Model and Thinking effort, and each row shows `account · model effort`.
  Any switch restarts the tab's Claude in place, on the same conversation
  (`--resume`, with `--model` for a model and `--effort` for an effort), and
  all together are one restart. Claude's efforts are low, medium, high,
  xhigh and max; a Codex tab gets `-c model_reasoning_effort=<effort>` and
  the efforts its model lists (Terra also has ultra). "default" leaves it to
  the program's own setting. Not while Claude works, asks something
  or still runs agents. Claude's own settings are not touched. The tab shows
  the model that answered last, read from its transcript.
- Tabs of other tools (Grok, agy) have no chat: only Claude's and Codex's
  sessions are read.
- When the tab starts a new conversation, the chat says so and goes on with
  the new one.
- **Menus on the tab's screen.** A tab that waits on a menu (the trust
  question for a folder, a permission question with numbered choices) shows
  as a card too, with the whole prompt above the menu (a long command in
  full; a prompt taller than the screen says its top is cut). Tap a choice
  and **Answer**, and tabit presses the number, or the arrow keys and Enter,
  in the tab. Before every key it reads the menu again, and stops if it has
  gone. **Skip** picks a numbered No; on a menu without numbers (the trust
  question) it presses nothing and leaves the menu to the tab.
- **Questions from the tab.** When the tab's Claude asks you to pick from a
  list, the chat shows the question as a card. Tap an option for each
  question and tap **Answer**; tabit presses the keys in the tab. **Skip**
  presses Esc. Only single-choice questions come as cards (up to 4
  questions of up to 4 options); answer other kinds in the tab. The card goes
  away when the question is answered in the tab. While it is open, the chat
  does not type into the tab.

## Tabs talk to each other

A Claude tab can send a message to another Claude tab. When a line of its
reply starts with `@name`, tabit types the whole reply into that tab, marked
`[from @me]`; the other tab answers the same way, with a line that starts
with `@me`. Each tab's name is its tab name made into one word (`[Claude] LPR`
is `@claude-lpr`; two of the same name get `-2`, `-3`).

- Every Claude tab is told, with each prompt, its own name and the other
  tabs it can call, through tabit's hooks. A tab that is alone is told
  nothing.
- A Codex tab can be called too, and a line of its reply that starts with
  `@name` is passed on the same way. Codex is not told the names (it has no
  such hook), so it calls only when a message asks it to.
- Only replies written from now on count: what a tab said before is not sent.
  A line inside a code block, or a file name like `@tabit.py`, is no call.
- There is no limit on how often tabs answer each other. Two tabs that keep
  replying to each other go on until you press Stop (Esc) in one of them.
- A tab that is asking you something does not take a message; the sender's
  chat says so.
- Two tabs in the same group talk through the group when they use the
  group's names (see Groups). With their tab names (which differ when two
  tabs have the same title) they talk directly, without the group's 8-pass
  pause.
- It runs from the time tabit starts, with or without the chat page open.
- **You can call tabs too.** A message that starts with `@name` (or several:
  `@ann @ben ...`, with or without commas) goes only to those tabs, as in a
  group, from any chat; the chat shows it with "to @name". Their answers stay
  in their own chats. A message that names no tab first goes to the chat as
  always.
- Type `@` in the text box and a list of the other tabs' names comes up.
  Keep typing to narrow it; tap a name (or Enter or Tab) to put it in. In a
  group, the list is the group's members.

## Groups

A group puts two or more chats in one room.

- Your message goes to everyone. `@name` at the start sends it to one (or
  several: `@ann @ben ...`, with or without commas).
- A chat can talk to another: when its reply has a line that starts with
  `@name`, tabit passes that message on. This is how chats talk to each other.
- After 8 such passes the group stops and waits for you, so two chats can never
  talk forever. Anything you say starts the count again.
- A chat that is a tab joins like any other: the group's messages are typed
  into its tab, and what the tab's Claude replies comes back into the group.
  The tab's past (what was said before it joined, or before a new
  conversation) is not passed on.
- Questions from any member show in the group. Stop stops all of them.
- Deleting a group keeps its chats.

Turning off **Allow typing into tabs** stops new messages, but a chat that is
working goes on until it is done. Press **Stop** first if you want it to end.

## How it works

Each chat runs one headless `claude -p --input-format stream-json
--output-format stream-json`. Its events become chat events; nothing reads a
terminal screen, except the AI tabs above. The code is in `tabit_chat.py` (no
GTK), `chat.html` and `tabit-tabs`. The routes are `/` (and `/chat`),
`/chat.webmanifest`, `/api/chats`, `/api/chat-groups` and `/api/dirs/new`,
behind the same checks as the rest of Remote. A mirror reads the tab's Claude
transcript every 2 seconds.
