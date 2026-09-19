# open-computer-use ⌘

**A macOS computer-use agent with interchangeable model backends and a dynamic, indexed action space.**

Run it with an OpenAI-compatible model or the optional Jev backend. The macOS
accessibility bridge and execution loop run independently of Codex.

The tree first: no screenshots, no coordinates, and it works on a window you
are not looking at. Pixels only when an app publishes nothing — and then it
says so, and takes the screen to do it.

A macOS port of [browser-use/jev-ultrafast](https://github.com/browser-use/jev-ultrafast), which
put the same idea on the web: give the model a numbered table of what it can
actually do, and let it **choose** rather than generate. On the desktop the
table comes from the accessibility tree instead of the DOM — the same tree a
screen reader uses, which every native app already publishes.

```text
[1] TextArea    Untitled            · this line should be replaced
[2] Button      Close window
[3] CheckBox    Wrap to page        · checked
[4] PopUpButton Paper size          · A4
[m7] Format > Make Rich Text
```

Elements the app does not expose an operation for never appear. A checkbox is
never offered as a place to type. A disabled button is not a choice.

## The loop

```text
                       one request
                     ┌──────────────────────────────┐
accessibility tree → │ operation                    │
  → element table    │ press_target                 │
                     │ type_text_target + its value │
                     │ select_target / menu_target  │
                     │ risk                         │
                     └───────────────┬──────────────┘
                   use the head that matches the operation
                                     │
                     PRESS [2] ──────┤──→ AXPress, by path
                 TYPE_TEXT [1] ──────┤──→ AXValue, no keystrokes
                      MENU [m7] ─────┘──→ the command, menu still closed
                                     │
                              observe again
```

Operations: `PRESS`, `TYPE_TEXT`, `SELECT`, `MENU`, `CLICK`, `SCROLL_UP`,
`SCROLL_DOWN`, `PRESS_RETURN`, `PRESS_ESCAPE`, `WAIT`, `DONE`, `BLOCKED`.

`CLICK` is the odd one. It is addressed to an element out of the same table as
`PRESS`, and delivered with the pointer like the screenshot operations, so it
needs the app in front and is offered only with `activate`. It exists because
an element the app will not act on is still a place on the screen: a song in
Music is an `AXStaticText` inside an anonymous cell inside an anonymous row,
and not one of the three implements `AXPress`. Offering only what publishes an
accessibility action read that window — 14,274 nodes, 3,791 of them named text
— as 72 elements, none of which was a song. See "What a window is allowed to
say".

### What is not offered

`AXShowMenu` opens an element's context menu — the one a right-click opens —
and it works. It is still not offered, for two reasons found by running it.
macOS activates an app to show a menu, so the operation that needs no pointer
takes the screen instead; Finder went from `active: False` to `active: True`
across a single call. And the menu it opens is a separate window that a
snapshot of the focused window cannot see, so there is no way to report whether
it worked, or to choose anything in it afterwards.

`AXScrollToVisible` is published by almost every node in a web view and is
useless here for the opposite reason: offscreen elements never reach the table
in the first place, so everything that could be scrolled to is already in view.

`INCREMENT` and `DECREMENT` are the expensive lesson. The bridge has derived
and executed both from the start; adding them to the offered set is one line,
they work, and they take no screen. Calculator publishes them on "Show Sidebar"
and "Mode". Offering them put two more heads in every answer and two more ways
to be wrong in a task that needs neither, and on "compute 12 times 34" against
`deepseek-flash` the score went from **6 of 6 runs correct in 6 operations** to
**1 of 8**, the rest wandering into the 40-operation budget. The element table
was byte-for-byte identical in both; the only difference was the size of the
choice. An operation that is free to implement is not free to offer.

Every target head is answered on the same observed state, and only the head
matching the chosen operation can execute. Two decisions, one round trip —
[jev-ultrafast's speculative fan-out](https://docs.typesafe.ai/patterns/fan-out),
with the field's value speculated in the same request.

## The guard rides along

The riskiest thing about desktop automation is that a wrong press is not a
wrong page — it is a sent message, a deleted file, a granted permission.

So the same request that picks the operation also rates it. `risk >= 0.5` holds
the operation and hands it back instead of running it:

```python
with Agent("Mail", "Reply to the top thread with a note that I am travelling") as agent:
    for state in agent.run():
        ...
    if agent.state["status"] == "needs_approval":
        decision = agent.state["decision"]
        print(decision["operation"], decision["risk"], decision["risk_reason"])
        # → PRESS 0.9 "sends a message on the user's behalf"
        agent.approve_pending()   # only if you mean it
```

Pass `approve=lambda decision: ...` to answer programmatically, or raise
`risk_threshold`. Nothing consequential runs while the default is in place.

This costs no extra round trip. A separate safety reviewer is a second model
call on the critical path; a rating in the same structured answer is free.

## Try it

```bash
git clone https://github.com/max1874/open-computer-use.git
cd open-computer-use
uv sync
cp .env.example .env     # add JEV_API_KEY, or DECISION_API_KEY, or both
uv run open-computer-use
```

The inspector opens on **http://127.0.0.1:8767**: the numbered element table,
the operation and its probability distribution, the risk rating, and every
operation that actually ran. **Step** decides and executes one at a time.

Grant **Accessibility** to the terminal running this (System Settings > Privacy
& Security > Accessibility). Nothing here needs Screen Recording, because
nothing here takes a screenshot.

There are two decision backends, and which one answers depends on whether the
window can be described in text.

**jev**, a System One model, answers a set of named questions — one choice of
operation, one choice of index per operation, one score for the guard — and
returns a probability distribution with each. Nothing is asked to write JSON,
because nothing is asked to write. It takes text only and produces no text, so
`TYPE_TEXT` values come from the text model below.

```bash
JEV_BASE_URL=https://api.typesafe.ai/v1
JEV_MODEL=jev-latest
```

**Any OpenAI-compatible chat endpoint**, used for every decision when
`JEV_API_KEY` is unset, and for the screenshot fallback either way — a window
with nothing in its tree has to be shown as a picture, which jev cannot take.
A provider that constrains the answer server-side (`json_schema`) is used in
strict mode; one that only guarantees valid JSON gets the same shape spelled
out in the prompt.

```bash
DECISION_BASE_URL=https://openrouter.ai/api/v1
DECISION_MODEL=deepseek/deepseek-v4.1-flash   # 1M context, logprobs, takes images
```

Either way an answer outside the offered choices is refused and nothing runs,
so the constraint is never the only thing standing between a model and your
machine. jev returns its distribution directly; a chat provider that returns
logprobs gets one reconstructed from them, and one that returns neither shows
the model's own confidence instead.

## Use the library

```python
from open_computer_use import Agent

with Agent("TextEdit", "Replace the text with a haiku about the menu bar.") as agent:
    for state in agent.run():
        print(state["elapsed_ms"], state["status"])
```

```bash
uv run --env-file .env python examples/calculator.py   # six presses, verified
uv run --env-file .env python examples/textedit.py     # generate and enter a value
uv run --env-file .env python examples/run.py \
  --app "System Settings" --goal 'Turn on Dock auto-hide.' --activate

uv run python examples/electron.py --app Lark   # no model, no key: it only looks
```

```text
  1141 ms  PRESS  1               model 1000 ms   risk 0.01
  1743 ms  PRESS  2               model  457 ms   risk 0.01
  2369 ms  PRESS  Multiply        model  497 ms   risk 0.01
  2998 ms  PRESS  3               model  497 ms   risk 0.01
  3663 ms  PRESS  4               model  529 ms   risk 0.01
  4356 ms  PRESS  Equals          model  540 ms   risk 0.01

done after 6 operations, 5162 ms
  7 model calls, 4133 ms of it waiting on the model (80% of the wall clock)
  display                         : '12×34\n408'
  the app never came to the front : True
```

The first answer costs about twice what the rest do, because a connection is
being made: on jev that held in all eight runs, between 1.9 and 2.6 times the
median of the rest. The chat arm is noisier about it — 1.1 to 4.5 — but the
typical run is worse, not better. It is the one number here that a warm process
would not pay.

`--activate` brings the app to the front. Without it nothing moves on your
screen — the agent reads and presses a window you are not looking at. The one
thing that needs the front is the menu bar: an inactive app reports every menu
command as disabled, so menu commands are simply not offered until the app is
active.

## Why it moves

- **One request per decision cycle.** The operation head and every target head
  share one observed state.
- **The tree, not the pixels.** An accessibility snapshot of a TextEdit window
  is 19 ms and a few hundred tokens. A screenshot is an image, a resize
  sensitivity, and a coordinate the model has to be right about.
- **One long-lived bridge.** The Swift helper stays up for the session, so a
  step costs one traversal, not a process launch.
- **Act by path, guarded.** Every executed target came from an observed
  element, and the element at that path must still mention what was chosen —
  otherwise the operation is refused, not guessed. Trees shift between
  observing and acting.
- **Erase, set the value, read it back.** `TYPE_TEXT` means "replace the whole
  value", and writing the value is not a replace everywhere: a rich text
  composer treats it as an insert and keeps what was there. So the old contents
  are erased through the keyboard first — aimed only at an element the app is
  confirmed to be focused on — and only then is the value written and read
  back. Erasing first is also what makes the read-back mean anything: a field
  that already held the text would pass the check whether or not the write
  landed. None of it takes the screen. Keyboard typing is the fallback when the
  value is refused, and the result says which mechanism ran.
- **Menu commands without opening menus.** The whole menu bar is a flat,
  addressable action space — something a browser agent has no equivalent of.
- **Semantic freshness.** A fingerprint over roles, labels, values and states,
  not a mutation count. A window that merely moved has not changed.
- **Visible text only.** Offscreen and zero-size elements never reach the
  model's context.

On this path, model output never becomes a path, a selector, a shell command or
executable code. It selects an index from a table the executor built.

## What a window is allowed to say

The second time this project mistook its own reader for the app.

The first is the section below: Electron windows looked empty because the walk
stopped above the web content. This one is Music. Its window publishes 14,274
accessibility nodes — 955 rows, 6,595 cells, 3,791 named static texts carrying
every song title, artist, album and duration, and every entry in the sidebar.
This reader offered **72 elements**, and not one of them was a song.

The filter was one line: an element with no accessibility action is context,
not a choice. It sounds conservative. What it means in a list-shaped app is
that the contents of the window are unreachable, because a song in Music is an
`AXStaticText` inside an anonymous `AXCell` inside an anonymous `AXRow` and
none of the three implements `AXPress`. What survived the filter was 67 hover
buttons — ten identical triples of 喜爱 / 播放 / 更多, carrying no identity at
all — so a run that needed a particular song had a table with plenty in it and
nothing to aim at. The sparseness test called that window rich, which it was,
and not addressable, which it could not see.

So elements that carry content are now offered too, with `CLICK`, which aims
at the middle of the element's own rectangle. Three things keep it honest:

- **It is last.** Any element that answers to the accessibility API is reached
  that way; `CLICK` appears only where nothing else can.
- **It costs the screen.** Delivery is a real pointer event, so the app must be
  in front, which makes it `activate`-only — the same permission as the
  screenshot operations, and for the same reason. Without `activate`, Music
  reads as 71 tree-only elements and nothing takes the screen.
- **Content and controls have separate budgets.** A shared limit is spent by
  four thousand song titles before the walk reaches the toolbar, and the table
  then looks full while the app's actual controls have fallen off the end.

A static text's name is its value, which is its own small correction: every
other role answers to a title, a description or a label element, and reading
this one the same way returned an entire window's contents as anonymous.

## When the window says nothing

Far fewer windows than this project first claimed. An earlier version of this
section said Electron apps — Feishu, Lark, Slack, VS Code — keep their interface
out of the accessibility tree entirely, and cited Feishu publishing two
characters of text and nowhere to type. That measurement was real and the
conclusion drawn from it was wrong: the tree was there and the walk stopped
above it. Chromium puts the web area nine levels below the window and the
interface another ten to twenty below that, and the traversal limit had been set
for native windows, which put everything within a dozen. Feishu at depth 18
reports 2 characters; at depth 40 it reports 2431 characters and a composer to
type into. Lark reports 3399 characters and 174 actionable elements.

The cost of looking that far is about 100 ms per snapshot on the apps that need
it (Lark 34 ms to 169 ms, Feishu 43 ms to 132 ms) and nothing measurable on the
apps that do not — Calculator and Finder return identical trees at depth 18 and
depth 60. Next to a decision the model takes most of a second to make, that is
a cheap way to read a window instead of photographing it.

What survives is a much smaller class. Linear publishes three elements, no text
and nothing to press, at any depth. For a window like that the tree really has
nothing to offer.

`examples/electron.py` reads one window at four depths and prints what each one
saw, so none of this has to be taken on trust. It presses nothing, types
nothing, activates nothing and calls no model:

```text
 depth  elements  named  coverage    text  typeable  sparse?     ms
    18        37     32       1.0       5         0    False     48
    26       101     54       1.0    2127         1    False    124
    40       205    126       1.0    6030         1    False    332
    60       205    126       1.0    6030         1    False    345
```

Run it against a native window to see the control: Finder reports the same 40
elements and 1000 characters at every depth, because it never had anything
below twelve levels to find.

The `sparse?` column is worth reading closely, because on this window it is
wrong in the comfortable direction. The shallow read never measured as sparse —
coverage stays at 1.0, carried by a single element spanning the window — so the
screenshot fallback would not have fired either. It was an empty table that
nothing flagged, which is exactly how a measurement ends up supporting the
wrong conclusion.

So there is a second path, entered only when a window is sparse by measurement
(little of the window described, almost no text, nothing editable — see
`desktop.sparseness`): capture the window and offer `CLICK_POINT` and
`TYPE_KEYS` alongside the indexed elements.

**It is worse in every way, and it is meant to be a last resort.**

- The model **invents a coordinate** instead of selecting an index, so nothing
  can check the target before the click lands. This is the opposite of the idea
  the rest of the project is built on.
- **Clicking** it takes the screen, as implemented here. Seeing it does not.
  `pixels=True` photographs the window and shows it to the model with the app
  wherever it was; `activate=True` is what adds the operations aimed at that
  picture, because the click is delivered through the window server, which has
  one cursor. The two were one flag, and the cost of that was a window whose
  tree says nothing could not be looked at without being raised.

  The frontmost requirement belongs to how the click is sent, not to macOS:
  keyboard events go to a process and reach a background window, and whether a
  *mouse* event posted the same way can be made to land has not been tested
  here. Until it is, the pointer stays behind `activate=True`.
- It **sends your screen to the model.** The whole window, whatever is in it.
- It **cannot be verified by the tree.** These windows' fingerprints barely
  move whatever happens inside them, so captures carry a 16×16 greyscale
  reduction and a pixel operation is judged by comparing two of them. The
  comparison is per region, not per window: averaging the whole thing divides
  a real change by the unchanged majority around it, which scored a pressed
  digit at 0.30 and a third of a window changing at 1.88, both under a
  threshold of 2 and both therefore recorded as nothing happening. Scoring
  sixteenths of the window and taking the loudest separates cleanly — 0.00 to
  0.19 when nothing happened, 2.25 and up when something did. On the path
  itself, in Linear: four real clicks on empty space score 0.00, and clicking
  a tab scores 2.50 against the whole-window average's 0.41.

Three guards stand in front of it. The app must be frontmost. The point must
not be occluded — the window list is ordered front to back, so the code can ask
what a click at that point would actually hit. That guard exists because during
development a click aimed at Calculator landed in a browser window covering it.
And the window must still be the one in the picture, at the size it was: a
point means nothing except in the picture it was named in, so the capture's
window id and size ride along with it and a window resized in between is
refused. The bridge always had that check. Nothing sent it the window's
identity until it was tested, so for its whole life it could not fire — a
stale click went through and landed where the arithmetic happened to put it.

## Evidence and limits

Measured on this machine (M-series, macOS 26), median of 7, end to end from
Python — the JSON-RPC round trip to the Swift helper included, because that is
what a caller pays. An earlier version of this table reported much smaller
numbers for the same work, from inside Swift; those are not what anyone
experiences and are not comparable to these.

| | |
|---|---|
| snapshot, TextEdit window (4 elements, 2 levels) | **19.2 ms** |
| snapshot, Calculator window (24 elements, 5 levels) | **67.6 ms** |
| snapshot, Finder window (47 elements, 7 levels) | **116.7 ms** |
| snapshot, Lark window (201 elements, 39 levels) | **281.4 ms** |
| freshness check, TextEdit / Calculator / Finder / Lark | **16.6 / 77.4 / 124.9 / 253.4 ms** |
| `TYPE_TEXT` into a native document (TextEdit) | **152 ms** |
| `TYPE_TEXT` into a web composer (Lark) | **207 ms** |
| one whole step, act + settle + observe (Calculator) | **134 ms** |
| bridge start, once per session | **149 ms** |

The freshness check is not the cheap one it was described as. It asks the same
question as a snapshot and returns less of the answer: same traversal, same
accessibility reads, and those are where the time goes. It comes in within
noise of a full snapshot on every window measured. A genuinely cheap staleness
check would have to be a different question.

Both backends on the same task, `examples/calculator.py` — press `1`, `2`, `×`,
`3`, `4`, `=` and then notice you are done — eight runs each, same machine,
same hour, changing nothing but `JEV_API_KEY`:

| | jev | deepseek-v4.1-flash |
|---|---|---|
| correct result, verified by reading the display | **8 / 8 runs** | **8 / 8 runs** |
| operations per run | 6, every run | 6, every run |
| decision latency, median | **490 ms** | **2268 ms** |
| decision latency, range | 460-552 ms | 1656-3598 ms |
| task wall clock, median | **5.1 s** | **23.7 s** |
| share of wall clock waiting on the model | 78% | 80% |
| runs that brought the app to the front | 0 | 0 |

The accuracy columns are identical, so nothing here says one backend chooses
better than the other on a task this size. What separates them is how long the
answer takes, and one caveat has to be read alongside that number: the chat arm
goes through OpenRouter, and an earlier measurement of the same model class
against its vendor's own API was 744 ms. Most of the 4.6x is the gateway rather
than the model, and against that 744 ms the honest figure is about 1.5x.

Eight runs is a small sample and it is worth saying what a small sample hides.
An earlier version of this table said 6 of 6 and one before it 4 of 4, and a
build differing only by offering two more operations scored 1 of 8 - see "What
is not offered". Those earlier numbers were also taken with a weaker check than
this one: Calculator keeps a visible tape of previous calculations, and looking
for the expected value anywhere in the window passed on a result left behind by
the run before it. The check now reads the last line only, and the tape is
hidden before the run starts.

**Four fifths of every run is spent waiting for the answer, on both backends.
The macOS side of a whole step is 134 ms. The fastest answer measured here is
490.**

That is the argument for a System One model, and the reason jev-ultrafast runs
on one. Nothing in this loop is waiting on macOS; it is waiting for a decision
that was always a choice from a list. Asking for it as a choice rather than as
a JSON object someone has to write out is worth the better part of a second
every step, and it is the same six presses either way.

`scripts/check_bridge.py` reproduces the accessibility half with no model calls
at all.

## Not taking the model's word for it

`DONE` is the model's opinion about its own work. Give the agent a way to
check and the check decides instead:

```python
with Agent("Calculator", "Compute 12 times 34",
           verify=lambda page: "408" in page["text"]) as agent:
    ...
agent.state["verified"]    # True, False, or None if nothing could judge it
agent.state["status"]      # "blocked" when the model said DONE and the check said no
```

Three separate facts are now recorded per step, because they are three
different things and conflating them is how an agent comes to believe its own
press release:

- **dispatched** — the operation was sent. A reply that never comes back raises
  `UnknownOutcome`: it may have run, so the run stops rather than retrying.
- **window_changed** — something visibly moved. Not success.
- **verified** — the caller's check agrees the goal is met.

The same applies to staying out of your way. Every operation records the
frontmost process before and after, so `took_focus` is a measurement rather
than an architectural promise — `examples/calculator.py` prints `False` for it
on every run.

Known limits:

- **Extended thinking has to be off.** A model that reasons before answering
  spends its output budget doing it: a `deepseek-flash` answer measured here
  was 417 reasoning tokens to 13 tokens of JSON, and on a real action space it
  is the JSON that gets truncated. Disabled automatically for DeepSeek; check
  your provider's default before blaming the loop.

- **Menu titles do not revalidate.** After a command flips a menu item's title
  ("Make Rich Text" → "Make Plain Text"), the accessibility tree kept reporting
  the old title for at least two seconds in testing. Menu commands whose titles
  are state-dependent are unreliable; stable ones are fine.
- **Labels move under you.** Calculator's clear button is "All Clear" when the
  display is clear and "Clear" when it is not. That is exactly what the
  execution guard is for, and exactly why a plan made two observations ago
  cannot be trusted.
- **macOS terminates idle background apps.** An app the agent is driving but
  nobody is looking at can be reclaimed between runs. If the window disappears
  mid-run the operation that already executed stays on the record and the run
  stops as blocked, rather than vanishing with an exception.
- Web content is reachable but this is not a browser tool. Chrome reports 76
  elements and 3525 characters of text here, and forms in it read back fine, so
  the old claim that browser content is mostly absent from the tree was wrong
  in the same way the Electron one was. What is missing is everything a browser
  needs beyond one window: tabs, navigation, multiple pages. For web work use
  [browser-harness](https://github.com/browser-use/browser-harness) or
  [jev-ultrafast](https://github.com/browser-use/jev-ultrafast).
- **The pixel fallback works, and how well depends on the picture, not the
  model.** A whole run goes through it on Linear, which publishes three
  elements and no text at all: capture, coordinate, HID click, and the change
  read back off the picture. Asked to switch a tab, `deepseek-v4.1-flash`
  named a point 3 pixels from the centre of a tab whose position had been
  confirmed by clicking it, six times out of six, and the executed run landed
  on it. The same question against the old 1000-pixel capture missed by 40 to
  67 pixels every time — on that window, a different control. The interface
  and the model were identical; only the width changed. See `Desktop.capture`.
  Since the depth fix the fallback fires far less often than it was built to,
  which means it still gets far less exercise than the tree path.
- One window at a time: the focused window of one app. No sheets belonging to
  other windows, no multi-app workflows, no drag, no canvas, no web views.
- Apps that publish a poor accessibility tree cannot be driven well. Before
  concluding that an app is one of them, check that the walk is reaching its
  content: that mistake is the subject of "When the window says nothing" above.
- **`SELECT` is implemented and has never been offered.** It enumerates a
  pop-up button's options from its accessibility children, and a target set
  that comes out empty removes the operation from that element. Every pop-up
  and menu button open on this machine was counted once: **40 of them across
  Finder, Music, OrbStack, ChatGPT and Chrome, and 0 exposed a single menu
  item.** Seven had children at all, and those children were the button's own
  face — `AXImage`, `AXStaticText` — never an `AXMenuItem`. So the operation
  has no targets anywhere, and `PRESS` is what gets offered on those elements
  instead. All 40 advertise `AXShowMenu`, and pressing does open the menu; what
  is missing is reading it afterwards, because an open menu is its own window
  and the snapshot reads the focused one. That is the same gap that keeps
  `AXShowMenu` out of the action space, and closing it would revive both.
  Two things this measurement does not say. It does not say the reader is
  wrong: it already unwraps the classic AppKit shape, a lone `AXMenu` child
  whose children are the items. And it does not explain the emptiness, because
  36 of the 40 were web-view buttons and the other 4 were native toolbar
  `AXMenuButton`s — not one classic `NSPopUpButton` was on screen, so whether
  one of those would publish its items while closed is untested here. What is
  measured is the count.
- The risk rating is a model's judgement, not a policy engine. It is a gate on
  obvious harm, not a guarantee.
- **An editable field is not always a place to write prose.** A Finder window
  and an open dialog publish every filename as an ordinary `AXTextField` with a
  settable value, indistinguishable from a search box, so `TYPE_TEXT` aimed at
  one is a rename. Both were renamed by accident while this was being built;
  neither reached disk, because the name is only committed on Return. The guard
  that exists for this is the risk rating and `approve`, which means the guard
  is a model's opinion — worth knowing before running unattended somewhere with
  a file browser open.

## Small enough to read

| File | Job |
| --- | --- |
| [`axbridge.swift`](open_computer_use/axbridge.swift) | The accessibility snapshot, the indexed table, and guarded execution |
| [`agent.py`](open_computer_use/agent.py) | The loop, the guard gate, and the staleness handling |
| [`model.py`](open_computer_use/model.py) | The action space and the one request that fills every head |
| [`desktop.py`](open_computer_use/desktop.py) | The long-lived bridge and the settle policy |
| [`questions.py`](open_computer_use/questions.py) | The instructions and the budgets |
| [`demo.py`](open_computer_use/demo.py) | The local inspector |

`scripts/build.sh` compiles the bridge with `swiftc` and no dependencies.

## Credit

The design is [jev-ultrafast](https://github.com/browser-use/jev-ultrafast)'s:
the indexed action space, the speculative target heads, the scoped freshness
guards, and the rule that the model chooses rather than generates. That project
is by [Browser Use](https://github.com/browser-use) and runs on
[TypeSafe's Jev](https://docs.typesafe.ai/introduction).

This port began on an OpenAI-compatible model, with the decision written as a
single structured answer over enumerated choices — which is the shape a System
One model takes, so Jev now sits behind `model.choose` and answers it directly.
It still runs without a Jev key: leave `JEV_API_KEY` unset and every decision
goes to the chat backend, which is also where the screenshot fallback goes
either way, since Jev takes text only.

The accessibility helpers follow `cu`, a macOS accessibility CLI, MIT.

MIT.
