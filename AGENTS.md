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
- No screenshots in the loop. If you find yourself reaching for pixels, the
  answer is a better tree reader or an honest limitation.
- Do not take the screen. `activate` is opt-in and exists only because an
  inactive app will not validate its menu bar. Keyboard events go to the
  process, never to the system.
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
it, then `examples/textedit.py`.
