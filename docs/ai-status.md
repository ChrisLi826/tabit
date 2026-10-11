# AI agent status

How tabit tells you an agent finished or wants you. Back to the [README](../README.md).

## The popup

When an AI tab stops working — it finished, or it wants input — a popup
inside tabit says so, **at the foot of the tab list**, shaped like the tab
row it is about: the group's colour bar, the same status glyph, the group
and tab names, and the time. Click anywhere on it to switch to that tab.
When tabit is **not** the window in front, the same news also goes to the
desktop as a system notification, so it reaches you either way — the popup
is what is still waiting when you come back. Nothing pops for the tab you
are already looking at, and nothing pops for a status that changes back
within a few seconds, which is a detection wobble rather than news.

Opening a tab any other way takes its popup down too, and one tab never
has two: a second one replaces the first. **Nothing is ever thrown away
to make room.** Three of them show at a time, newest at the bottom; the
rest wait behind `3 more · 1 need input ▴`, which opens the whole list
oldest-first and folds it back again. **Clear all** takes down the
waiting ones as well. The AI counts in the row just below are never
covered, whatever the stack is doing.

One that goes away on its own only counts down while it is on screen and
tabit is in front, and never under the pointer — a popup that expired
where nobody could read it was never shown. The wheel over a popup
reaches the terminal underneath, since the stack hangs past a narrow tab
list.

It stays until you dismiss it, because urgency is the only say an app has
over the desktop half: GNOME gives every other notification four seconds
and ignores the timeout an app asks for. **Settings… → AI** turns it off
or drops it to one that goes away on its own. The desktop half needs
`gir1.2-notify-0.7`, which the installer pulls in; the in-app half works
without it.

## The count row

The foot of the sidebar counts **every** AI tab by status — `?` blocked,
`✓` done, play working, pause idle — next to a clock. The off-viewport
peeks only cover tabs scrolled out of sight, so with a long list an agent
waiting on you could still be missed; this row cannot miss one. Click a
count to jump to that agent, and click again to cycle through the rest.

The same news can go to your phone: see [telegram.md](telegram.md) and [remote.md](remote.md).
