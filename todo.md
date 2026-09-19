# Not done

README's "Known limits" says what this cannot do today. This file says which of
those are meant to move, what each one is actually blocked on, and what it would
cost. Nothing here is scheduled.

## Would remove a real constraint

**Deliver a click without taking the screen.** The pixel path needs the app in
front, and that is the one thing a user notices. What is implemented needs
activation; whether a *click* fundamentally needs it has never been tested. Key
events are already posted to the pid and land on a background window. The same
has never been tried with a mouse event, and if it works the `activate`
requirement on the pixel path goes away. One experiment: post a
`.leftMouseDown`/`.leftMouseUp` pair to a pid, on a window that is behind
another, and read the window back. Cheap, and it either removes the constraint
or turns an untested sentence in the README into a measured one.

**Read a menu after it opens.** Blocks two things at once: `AXShowMenu` is kept
out of the action space, and `SELECT` has never been offered — 40 pop-up and
menu buttons counted across five apps, 0 exposing a menu item while closed. An
open menu is its own window and the snapshot reads the focused one, so the work
is cross-window observation, not a new operation. Cost to weigh first: an open
menu holds the screen for as long as it is open. Do not start this without
saying that out loud.

**Give `SELECT` a post-execution read-back.** Only after the above. Today it
confirms that `AXUIElementPerformAction` returned success and reads nothing
back, unlike `TYPE_TEXT`. Pointless while the operation is never offered.

## Known gaps, no plan

- **Text input is not general.** `TYPE_KEYS` maps characters the keyboard
  layout can produce; CJK and emoji go through Unicode events. Key combinations,
  paste, and text selection are not addressable at all.
- **Drag has no representation.** Neither does a custom-drawn canvas.
- **One window, one app.** No sheets belonging to other windows, no workflows
  that cross apps. This is a design boundary, not an oversight — moving it
  changes what a snapshot is.
- **The pixel fallback gets little exercise.** Since the traversal-depth fix it
  fires far less often than it was built to, so it is the least-tested path in
  the project per line of code.
