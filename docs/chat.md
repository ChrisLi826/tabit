# Tabit Chat

Tabit Chat is Remote as a conversation. You talk to Claude on this computer
from a phone or another computer, in chat bubbles, not in a terminal. Back to
[Remote](remote.md).

Open `https://<this-host>.<tailnet>.ts.net/chat`. It needs the same switch as
typing into tabs (**Settings… → Remote → Allow typing into tabs**), because a
chat can run commands on this machine. On a phone, **Share → Add to Home
Screen** makes it an app of its own.

## What it does

- **Chats.** Each chat is one Claude working in a folder you pick, with a
  model you pick. Replies stream in as they are written. Markdown, lists and
  code show properly; a file path in `code` opens the file (when **Allow
  opening files** is on).
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

## Mirror a tab

New chat can **mirror a Claude tab** instead of starting a Claude of its own.
The chat then shows that tab's conversation, and what you type goes into the
tab. A tab that is working takes your line too: Claude holds it until the
turn is done. **Stop** presses Esc in the tab. The list marks the chat with
🔗 and the tab's name.

- Mode and model belong to the tab, so the chat has none. A mirror cannot
  join a group.
- When the tab starts a new conversation, the chat says so and goes on with
  the new one.
- **Questions from the tab.** When the tab's Claude asks you to pick from a
  list, the chat shows the question as a card. Tap an option for each
  question and tap **Answer**; tabit presses the keys in the tab. **Skip**
  presses Esc. Only single-choice questions come as cards (up to 4
  questions of up to 4 options); answer other kinds in the tab. The card goes
  away when the question is answered in the tab. While it is open, the chat
  does not type into the tab.

## Groups

A group puts two or more chats in one room.

- Your message goes to everyone. `@name` at the start sends it to one (or
  several: `@ann @ben ...`).
- A chat can talk to another: when its reply has a line that starts with
  `@name`, tabit passes that message on. This is how chats talk to each other.
- After 8 such passes the group stops and waits for you, so two chats can never
  talk forever. Anything you say starts the count again.
- Questions from any member show in the group. Stop stops all of them.
- Deleting a group keeps its chats.

Turning off **Allow typing into tabs** stops new messages, but a chat that is
working goes on until it is done. Press **Stop** first if you want it to end.

## How it works

Each chat runs one headless `claude -p --input-format stream-json
--output-format stream-json`. Its events become chat events; nothing reads a
terminal screen, except the AI tabs above. The code is in `tabit_chat.py` (no
GTK), `chat.html` and `tabit-tabs`. The routes are `/chat`,
`/chat.webmanifest`, `/api/chats`, `/api/chat-groups` and `/api/dirs/new`,
behind the same checks as the rest of Remote. A mirror reads the tab's Claude
transcript every 2 seconds.
