# jev-computer-use

Read README.md and docs/design.md before editing. Keep the loop small:
window → indexed elements → operation + target → guarded execution → observe.

- The input is one natural-language goal and one app. No per-app scripts, no
  hardcoded field values, no coordinates.
- One request chooses the operation and fills every target head on the same
  observed state. Consume only the head matching the chosen operation.
- Targets must map to observed elements and to operations those elements
  actually support. Model output never becomes a path, a selector, a shell
  command or executable code.
- Never retry a mutation. Log execution before observing its result.
- No screenshots on the tree path. Reaching for pixels because the reader is
  lazy is a bug; the fallback is for windows that genuinely publish nothing,
  and `desktop.sparseness` decides that by measurement, not by vibes.
- Adding an operation costs accuracy on every task that does not need it. The
  action space is the choice the model is making, so widening it is not free
  even when the new operation works perfectly. Offering INCREMENT and DECREMENT
  — two lines, correct, silent — took the calculator task from 6 of 6 runs to
  1 of 8. Measure a new operation against a task that does not use it before
  offering it, not only against one that does.
- A local change measured globally is not measured. Averaging a whole window
  divided every real change by the unchanged majority around it, so pressing a
  digit scored 0.30 against a threshold of 2 and a third of a window changing
  scored 1.88. Both read as nothing happened. Score the parts and take the
  loudest. Where the two mistakes cost differently, put the line nearer the
  cheaper one and say which it is.
- When a window measures as sparse, suspect the reader before the app. The
  first version of this project concluded from real measurements that Electron
  apps publish nothing, and was wrong: the traversal stopped above the web
  content. A measurement is evidence about the pair, not about the app.
- Do not take the screen on the tree path. `activate` is opt-in. Keyboard
  events go to the process, never to the system.
- Looking and aiming are separate permissions. `pixels` photographs a window
  that is behind everything else; `activate` is what offers the operations
  aimed at that picture, because a click as delivered here needs the app in
  front. Do not re-couple them.
- Never activate an app to unlock a code path you are not going to execute.
  Anything about what the model *would* choose — which operations are offered,
  which point it names, how a prompt change lands — is `choose` on a background
  capture, and costs the user nothing. Activating for that happened three times
  in one session before it was written down here; each time the measurement
  itself needed nothing but a picture.
- Never widen the pixel path's guards. A click must be on a frontmost,
  unoccluded window. A click aimed at a covered window lands in someone else's
  — that happened here once, and the guard exists because of it.
- The guard is part of the decision request, not a second call. Do not lower
  the default threshold to make a demo smoother.
- Verify outcomes by reading the window back. A `DONE` choice is not evidence,
  and neither is a successful API response.
- Keep README claims, measured numbers and what is actually verified in sync.
  Numbers produced by `scripts/mock_model.py` must be labelled as such — it
  proves the plumbing and nothing about model quality.
- Credentials stay in `.env`, which stays ignored.
- Do not commit or push unless asked.

Build: `scripts/build.sh`.
Accessibility checks, no model calls: `python3 scripts/check_bridge.py`
(add `--foreground` for the menu-bar checks).
Whole loop offline: run `scripts/mock_model.py`, point `DECISION_BASE_URL` at
it, then `examples/textedit.py`. It answers the pixel path too, against any
window whose tree is empty enough to trigger it — the only way to exercise
that path without a paid model, and the reason to keep it able to.
