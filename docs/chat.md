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
- **The main chat.** *Tabit Chat* (the owl) is always first in the list and
  cannot be deleted. It is your main assistant: bring it anything. For a big
  job that needs its own folder, it suggests that you start a separate chat.
- **Questions.** When Claude wants to run a command or change a file, the chat
  shows a card with **Allow** and **Deny**, and **Allow & don't ask again**
  when Claude offers a rule to remember. When
  Claude asks you a choice, the options are buttons. **Stop** ends the turn.
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
- **🔔** tells you when Claude is done while the page is hidden.
- **History.** Chats and their history stay on this computer in
  `~/.local/share/tabit/chats/`. Close the page, come back later, and it goes
  on. Each chat is a normal Claude session, so you can also `claude --resume`
  it at the desk, in the chat's folder.
- A chat that sits idle for 20 minutes lets its Claude process go; your next
  message starts it again where it left off. At most 6 run at once.

## Claude login

Every chat uses the same Claude login. When Settings → AI on the computer has
more than one Claude account, **🔑** at the top of the list shows which one
and lets you pick another. All the chats move to it and carry on where they
were, because every login shares the same sessions. While a chat is working
the switch is refused: wait, or press Stop. A chat that mirrors a tab does not
count, since it runs no Claude of its own.

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

- **One name, one group.** The chat has the tab's name and shows the tab's
  group under it, with the group's colour. Rename the chat and the tab is
  renamed. Rename or regroup the tab and the chat follows within 10 seconds.
- **New chat** opens a Claude tab in the group you pick, in the folder you
  pick. With no group picked, the tab is in no group.
- **Delete.** Deleting a chat closes its tab. The chat asks first. The last
  tab in tabit cannot be closed from the phone, so that chat stays. Closing a
  tab removes its chat after about a minute.
- **Old chats move into tabs.** A chat that ran its own Claude (not the main
  Tabit Chat) moves into a tab when it is idle: the tab goes on with the same
  conversation (`--resume`), keeps the login and bypass, and the chat that
  mirrors it takes the old one's picture and name, and its place in the
  groups (rooms) the old chat was in. The old chat's folder is kept as
  `~/.local/share/tabit/chats/<id>.moved`, history and all. Of the modes only
  Bypass goes along; a chat in Accept edits or Plan first becomes a tab that
  asks each time (unless **Settings → AI → Claude bypass** is on, which makes
  every new Claude tab bypass, this one too). A chat that cannot move is tried 3 times.
- **Account and model.** A chat's settings show Account and Model, and each
  row shows `account · model`. Either switch restarts the tab's Claude in
  place, on the same conversation (`--resume`, with `--model` for a model),
  and both together are one restart. Not while Claude works, asks something
  or still runs agents. Claude's own settings are not touched. The tab shows
  the model that answered last, read from its transcript.
- Tabs of other tools (Codex, Grok) have no chat: only Claude's transcript is
  read.
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
