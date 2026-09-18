# jev-computer-use ⌘

**A macOS computer-use agent with a dynamic, indexed action space.**

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

Operations: `PRESS`, `TYPE_TEXT`, `SELECT`, `MENU`, `SCROLL_UP`, `SCROLL_DOWN`,
`PRESS_RETURN`, `PRESS_ESCAPE`, `WAIT`, `DONE`, `BLOCKED`.

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
git clone https://github.com/max1874/jev-computer-use.git
cd jev-computer-use
uv sync
cp .env.example .env     # add DECISION_API_KEY
uv run jev-cu
```

The inspector opens on **http://127.0.0.1:8767**: the numbered element table,
the operation and its probability distribution, the risk rating, and every
operation that actually ran. **Step** decides and executes one at a time.

Grant **Accessibility** to the terminal running this (System Settings > Privacy
& Security > Accessibility). Nothing here needs Screen Recording, because
nothing here takes a screenshot.

Any OpenAI-compatible chat endpoint works. A provider that constrains the
answer server-side (`json_schema`) is used in strict mode; a provider that only
guarantees valid JSON gets the same shape spelled out in the prompt. Either
way an answer outside the offered choices is refused and nothing runs, so the
constraint is never the only thing standing between a model and your machine.

```bash
DECISION_BASE_URL=https://api.deepseek.com/v1
DECISION_MODEL=deepseek-flash      # V4.1-Flash: 1M context, logprobs, json_object
```

When the provider returns logprobs, the inspector draws the real distribution
over the operation head; otherwise it shows the model's own confidence.

## Use the library

```python
from jev_computer_use import Agent

with Agent("TextEdit", "Replace the text with a haiku about the menu bar.") as agent:
    for state in agent.run():
        print(state["elapsed_ms"], state["status"])
```

```bash
uv run --env-file .env python examples/calculator.py   # six presses, verified
uv run --env-file .env python examples/textedit.py     # generate and enter a value
uv run --env-file .env python examples/run.py \
  --app "System Settings" --goal 'Turn on Dock auto-hide.' --activate
```

```text
  1021 ms  PRESS  1                 model  812 ms   risk 0.00
  1933 ms  PRESS  2                 model  752 ms   risk 0.00
  3099 ms  PRESS  Multiply          model  979 ms   risk 0.00
  4034 ms  PRESS  3                 model  767 ms   risk 0.00
  4934 ms  PRESS  4                 model  714 ms   risk 0.00
  6273 ms  PRESS  Equals            model 1112 ms   risk 0.00

done after 6 operations, 7728 ms
  display                         : '12×34\n408'
  the app never came to the front : True
```

`--activate` brings the app to the front. Without it nothing moves on your
screen — the agent reads and presses a window you are not looking at. The one
thing that needs the front is the menu bar: an inactive app reports every menu
command as disabled, so menu commands are simply not offered until the app is
active.

## Why it moves

- **One request per decision cycle.** The operation head and every target head
  share one observed state.
- **The tree, not the pixels.** An accessibility snapshot of a TextEdit window
  is 4 ms and a few hundred tokens. A screenshot is an image, a resize
  sensitivity, and a coordinate the model has to be right about.
- **One long-lived bridge.** The Swift helper stays up for the session, so a
  step costs one traversal, not a process launch.
- **Act by path, guarded.** Every executed target came from an observed
  element, and the element at that path must still mention what was chosen —
  otherwise the operation is refused, not guessed. Trees shift between
  observing and acting.
- **Set the value, don't type it.** `TYPE_TEXT` writes the accessibility value
  directly: no keystrokes, no focus change, and it reads the value back before
  returning. Keyboard events are the fallback, and the result says which ran.
- **Menu commands without opening menus.** The whole menu bar is a flat,
  addressable action space — something a browser agent has no equivalent of.
- **Semantic freshness.** A fingerprint over roles, labels, values and states,
  not a mutation count. A window that merely moved has not changed.
- **Visible text only.** Offscreen and zero-size elements never reach the
  model's context.

On this path, model output never becomes a path, a selector, a shell command or
executable code. It selects an index from a table the executor built.

## When the window says nothing

Chromium-based apps — Feishu, Linear, Slack, VS Code — render their real
interface into a web content area that never reaches the accessibility tree.
What is left is the native chrome: a sidebar, the window buttons, all perfectly
well named and all useless. Feishu publishes 17 actionable elements covering 3%
of its window, with two characters of readable text and nowhere to type.

So there is a second path, entered only when a window is sparse by measurement
(little of the window described, almost no text, nothing editable — see
`desktop.sparseness`): capture the window and offer `CLICK_POINT` and
`TYPE_KEYS` alongside the indexed elements.

**It is worse in every way, and it is meant to be a last resort.**

- The model **invents a coordinate** instead of selecting an index, so nothing
  can check the target before the click lands. This is the opposite of the idea
  the rest of the project is built on.
- It **takes the screen**. A mouse event posted to a process is ignored by
  ordinary controls — Calculator's keypad does not react to one even when the
  app is frontmost — so a click that actually lands must go through the window
  server, which has one cursor. The app must be in front, the pointer really
  moves, and it is put back afterwards. `pixels` is therefore only available
  with `activate=True`.
- It **sends your screen to the model.** The whole window, whatever is in it.
- It **cannot be verified by the tree.** These windows' fingerprints barely
  move whatever happens inside them, so captures carry a 16×16 greyscale
  reduction and a pixel operation is judged by comparing two of them.

Two guards stand in front of it. The app must be frontmost, and the point must
not be occluded — the window list is ordered front to back, so the code can ask
what a click at that point would actually hit. That guard exists because during
development a click aimed at Calculator landed in a browser window covering it.

## Evidence and limits

Measured on this machine (M-series, macOS 26), median of 20:

| | |
|---|---|
| snapshot, TextEdit window (4 elements) | **4.2 ms** |
| snapshot, Chrome window (20 elements) | **11.5 ms** |
| freshness check (no element table) | **3.5 ms** |
| `TYPE_TEXT` execute, verify, and settle | **32 ms** |
| one whole step, act + settle + observe | **43 ms** |
| bridge start, once per session | **57 ms** |

With a real model, `deepseek-flash` over the public API, on
`examples/calculator.py` — press `1`, `2`, `×`, `3`, `4`, `=` and then notice
you are done:

| | |
|---|---|
| correct result, verified by reading the display | **4 / 4 runs** |
| operations per run | 6 |
| task wall clock | **7.5 s** (median) |
| decision latency | **889 ms** (median of 25 calls) |
| share of wall clock spent waiting on the model | **83%** |

**The harness is 43 ms per step. The model is 889 ms. Twenty to one.**

That gap is the whole argument for a System One model, and the reason
jev-ultrafast runs on one. Nothing in this loop is waiting on macOS; it is
waiting on a transformer generating a JSON object to say the word `PRESS`.
Swap the decision call for a model that returns a choice instead of writing
one out, and a six-press task stops being a seven-second task.

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
- Web content inside a browser is mostly absent from the accessibility tree.
  For web pages use [browser-harness](https://github.com/browser-use/browser-harness)
  or [jev-ultrafast](https://github.com/browser-use/jev-ultrafast); this is for
  native apps.
- **The pixel fallback has been verified on Calculator, not on a real Electron
  app.** Driving it through HID clicks works (7 × 3 = 21, by coordinate), the
  occlusion and frontmost guards refuse correctly, and the capture round-trips
  to the right screen point. Whether a model can reliably pick coordinates in a
  dense interface like a chat client is untested.
- One window at a time: the focused window of one app. No sheets belonging to
  other windows, no multi-app workflows, no drag, no canvas, no web views.
- Apps that publish a poor accessibility tree cannot be driven well, and this
  does not fall back to pixels. That is the trade.
- `SELECT` needs a pop-up button that exposes its menu while closed; many do
  not, and then only `PRESS` is offered.
- The risk rating is a model's judgement, not a policy engine. It is a gate on
  obvious harm, not a guarantee.

## Small enough to read

| File | Job |
| --- | --- |
| [`axbridge.swift`](jev_computer_use/axbridge.swift) | The accessibility snapshot, the indexed table, and guarded execution |
| [`agent.py`](jev_computer_use/agent.py) | The loop, the guard gate, and the staleness handling |
| [`model.py`](jev_computer_use/model.py) | The action space and the one request that fills every head |
| [`desktop.py`](jev_computer_use/desktop.py) | The long-lived bridge and the settle policy |
| [`questions.py`](jev_computer_use/questions.py) | The instructions and the budgets |
| [`demo.py`](jev_computer_use/demo.py) | The local inspector |

`scripts/build.sh` compiles the bridge with `swiftc` and no dependencies.

## Credit

The design is [jev-ultrafast](https://github.com/browser-use/jev-ultrafast)'s:
the indexed action space, the speculative target heads, the scoped freshness
guards, and the rule that the model chooses rather than generates. That project
is by [Browser Use](https://github.com/browser-use) and runs on
[TypeSafe's Jev](https://docs.typesafe.ai/introduction).

This port uses an OpenAI-compatible model for the decision, so it runs without
a Jev key. The decision call is a single structured answer over enumerated
choices, which is exactly the shape a System One model takes — dropping Jev in
behind `model.choose` is the obvious next step, and the text-value fallback in
`model.field_text` is already there for a backend that chooses but cannot
write.

The accessibility helpers follow `cu`, a macOS accessibility CLI, MIT.

MIT.
