# Dynamic operation + target, on the accessibility tree

The input is one natural-language goal and one app. Every observation builds an
indexed table of the elements that app currently exposes, together with the
operations each one actually supports. One node gets one index even when it can
be both pressed and typed into.

One request asks which operation to perform, and which target each available
operation would use. The executor consumes only the head matching the selected
operation, so a target that is incompatible with the operation cannot run. The
value for `TYPE_TEXT` is speculated in the same request; a decision backend
that can only choose falls back to a second, small text model.

Operation and target questions receive the same next-step rules. A target
question cannot read the operation answer, so its premise names the operation
it assumes.

## The snapshot

`axbridge.swift` walks the focused window once and produces, in a single
traversal: roles, names, values, checked and selected states, enabled state,
geometry, the operations each element supports, and the visible static text.
Zero-size elements and elements outside the window's bounds are dropped, so
offscreen content does not fill the model's context.

Names come from the title, the description, the label element pointing at the
control, the placeholder, the help text, or the nearest static text inside it —
in that order. Standard window controls are named from their subrole, because
their help text is a paragraph about zooming. This covers common labels; it is
not the system's full accessible-name algorithm.

Which operations an element offers is decided by role, not by whether a value
happens to be settable. Treating every control with a settable value as
editable misclassifies checkboxes — the same audit finding
[jev-ultrafast](https://github.com/browser-use/jev-ultrafast) reported for
`INPUT` elements.

The menu bar is read separately and flattened into `Menu>Item>Subitem` paths.
It is a second traversal, so it is cached for five seconds and invalidated by
any executed operation. Services, Open Recent, Share and similar submenus are
skipped: they are system-wide or history noise, never the goal.

## Identity and freshness

Elements are addressed by path — child indices from the application element —
because the accessibility API offers no stable node identity across
observations. A path is therefore a guess about a tree that may have moved, and
every execution carries the label the decision was made about. If the element
at that path no longer mentions it, the operation is refused rather than
performed on whatever is there now.

The freshness fingerprint is semantic: window title, plus role, label, value,
enabled, checked and selected for every element, plus the visible text.
Geometry is deliberately excluded, so a window that was merely moved or resized
is still fresh. The check runs without building the element table or reading
the menus, which makes it about as cheap as a snapshot minus serialisation.

Guards are scoped on purpose. Before a text value is generated and before a
`DONE` is accepted, the whole window must still be fresh. Before an individual
press, only the target element must still be the element that was chosen. That
is a practical heuristic, not proof that an unrelated change was irrelevant.

Execution is recorded before the result is observed, so a stale post-action
observation cannot erase an operation that actually ran. Nothing is retried: a
press that may have landed is never sent twice.

## Focus

Everything except the menu bar works on a window that is not in front. Reading
the tree, pressing by path and setting a value all go through the accessibility
API, which does not involve the pointer, the keyboard focus or the screen.

The menu bar is the exception. An inactive app does not validate its menus:
every item comes back disabled, and titles that depend on document state go
stale. So menu commands are offered only while the app is active, and bringing
it to the front is opt-in — it is the one thing the user will notice.

Keyboard events, when they are needed at all, are posted to the target process
rather than to the system, so they do not follow the user's focus.

## The decision request

Everything the model is asked goes in one request: the operation, a target for
each available operation, the value for `TYPE_TEXT`, and the risk rating. Only
the head matching the chosen operation can execute, so the rest cost a few
output tokens and no extra round trip.

A System One backend is asked for that directly: each head is a named question
answered with a choice and a distribution over the options, and the risk rating
is a score over three ordered levels rather than a number someone has to write
down. Nothing is asked to produce JSON, because nothing is asked to produce
text — which also means it cannot produce a `TYPE_TEXT` value, and that falls
to `model.field_text`, or be shown a screenshot, which sends the sparse-window
path to the chat backend instead.

A chat backend is asked for the same thing as one JSON object. Where the
endpoint can constrain the answer server-side it is asked to. Where it can only
guarantee valid JSON, the same shape goes in the prompt instead. Neither is
load-bearing: an operation or target outside the offered choices is refused
here, and nothing executes.

Extended thinking is turned off where it is on by default. Choosing from an
enumerated table is not a reasoning task, and a model that thinks first spends
its output budget doing it — one measured `deepseek-flash` answer was 417
reasoning tokens against 13 tokens of JSON. On a real action space it is the
JSON that gets truncated, and the run fails with nothing executed.

Which provider that applies to is read from the model name as well as the base
URL. It used to be read from the URL alone, which is correct until the same
model arrives through a gateway: `deepseek-v4.1-flash` served from
`openrouter.ai` is still DeepSeek, and every run through it failed in exactly
the way the paragraph above describes, by the one route the check could not
see.

## The guard

The request that chooses the operation also rates how consequential it is, and
says what it changes. Above the threshold the operation is held and returned
instead of executed, and the caller decides.

Putting the rating in the same structured answer is the point: an independent
safety reviewer is a second model call on the critical path, while one more
field is free. The cost is independence — the rating is the same model's
judgement about its own choice, which makes it a gate on obvious harm, not an
adversarial check.

Element text is data. A button titled "Approve and send" describes a button.

## Boundaries

A run is bounded at 40 executed operations and 80 decisions. At most 250
elements and 200 menu commands are offered; anything beyond that is truncated
and cannot be selected. Three consecutive operations that change nothing
visible end the run as blocked.

The inspector binds to loopback, checks Host and Origin, requires a token
minted at startup, and serialises commands because they drive a real
application.

## What is not solved

Multi-window and multi-app workflows are out of scope: one focused window of
one app. Drag, text selection and custom-drawn canvases have no accessible
representation to address and no operation here that reaches them.

There is a pixel fallback, and this paragraph said there was not for as long as
it existed. It is for a window whose tree is genuinely empty — capture, a point
named in the picture, a real click — and it is strictly worse than everything
else here: the point cannot be checked against anything before it lands, and
the delivery requires the app in front. It is described in the README under
"When the window says nothing".

Web content is not the gap it was recorded as either. Chrome publishes a usable
tree, forms in it read and write, and the earlier claim came from the same
traversal limit that made Electron apps look empty. What a browser needs beyond
that — tabs, navigation, more than one page — is what is missing.

An open menu is its own window, and the snapshot reads the focused one, so
nothing here can see a menu while it is open. Two things sit behind that.
`AXShowMenu` is not in the action space, and `SELECT` — which needs a pop-up
button's menu items and reads them as accessibility children — has never been
offered: of 40 pop-up and menu buttons counted across five running apps, none
exposed a menu item while closed, so the target set is always empty and the
operation removes itself. `PRESS` is offered on those elements instead and does
open the menu; what follows is unreadable. Why they are empty is not
established — the 40 were web-view and toolbar buttons, with no classic
`NSPopUpButton` among them.

Menu items whose titles change with document state kept reporting their old
title for seconds after the state changed, so state-dependent menu commands are
not reliable.

A valid operation can still be the wrong one. Whether a task succeeded is
decided by reading the window afterwards, not by the model's `DONE`.
