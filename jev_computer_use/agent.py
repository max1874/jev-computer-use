"""The complete loop: observe the window, choose one operation, execute it, observe again."""

import time

from .desktop import (
    PIXEL_CHANGE_THRESHOLD,
    BridgeError,
    Desktop,
    Executed,
    StaleWindow,
    UnknownOutcome,
    thumbnail_difference,
)
from .model import action_space, choose, field_context, field_text
from .questions import MAX_DECISIONS, MAX_STEPS, RISK_THRESHOLD


class Agent:
    """Drive one macOS app towards one natural-language goal.

    `approve` is called with the pending decision whenever the model rates an
    operation at or above `risk_threshold`. Without it the run stops at
    `needs_approval` and hands the decision back, rather than acting.

    Nothing here takes the screen: elements are read and pressed through the
    accessibility API, which works on a window the user is not looking at.
    `activate=True` brings the app to the front, which is the one thing the
    user will notice — ask for it only when the menu bar is needed, because an
    inactive app reports every menu command as disabled.
    """

    def __init__(
        self,
        app,
        goal,
        *,
        approve=None,
        risk_threshold=RISK_THRESHOLD,
        activate=False,
        menus=True,
        pixels=True,
        verify=None,
    ):
        goal = goal.strip() if isinstance(goal, str) else "\n".join(goal).strip()
        if not goal:
            raise ValueError("Supply a goal")
        self.approve = approve
        self.risk_threshold = risk_threshold
        # Called with the final observation when the model says DONE. Return
        # True to confirm, False to reject it, None if it cannot be judged.
        # Without one, DONE is only ever the model's own claim.
        self.verify = verify
        self.pending_text = None
        # Screenshots are a fallback for windows that publish nothing, not a
        # default input. Set pixels=False to keep the run tree-only.
        #
        # A real click goes through the window server, which has one cursor, so
        # the pixel path only exists for an app that is in front. Offering it
        # for a background window would be offering an operation that is
        # guaranteed to be refused.
        self.pixels = pixels and activate
        self.activate = activate
        self.capture = None
        self.desktop = Desktop(app, menus=menus, activate=activate)
        try:
            page = self.desktop.observe()
        except Exception:
            self.desktop.close()
            raise
        self.state = dict(
            app=app,
            goal=goal,
            page=page,
            decision=None,
            history=[],
            decisions=[],
            status="ready",
            verified=None,
            # `activate=True` raises the app before the first observation, and
            # that is the run taking the screen as surely as any step in it.
            # Summing this over the steps alone reported False for a run that
            # had already brought the window forward to begin with.
            took_focus=bool(activate),
            activated_at_start=bool(activate),
            elapsed_ms=0,
            started_at=None,
        )

    def snapshot(self):
        elements, targets, _ = action_space(self.state["page"])
        return {
            **{k: v for k, v in self.state.items() if k != "page"},
            "window": {
                k: self.state["page"][k] for k in ("app", "window", "fingerprint", "text", "truncated")
            },
            "elements": elements,
            "menus": self.state["page"].get("menus", []),
            "targets": {k: sorted(v) for k, v in targets.items()},
        }

    def _elapsed(self):
        return round((time.perf_counter() - self.state["started_at"]) * 1000)

    def command(self, name, body=None):
        body = body or {}
        state = self.state

        if name == "tick":
            try:
                self.command("predict")
                if state["status"] == "needs_approval":
                    return self.snapshot()
                return self.command("act", {"fingerprint": state["page"]["fingerprint"]})
            except StaleWindow:
                # The window moved on between deciding and acting. Nothing ran.
                state["decision"] = None
                state["status"] = "ready"
                state["page"] = self.desktop.observe()
                state["elapsed_ms"] = self._elapsed()
                return self.snapshot()

        if name == "predict":
            if state["status"] in {"done", "blocked"}:
                raise ValueError("This run has stopped. Start a fresh agent.")
            if state["started_at"] is None:
                state["started_at"] = time.perf_counter()
            # Running out of budget is a way for a run to end, not a way for it
            # to break. Raising here threw a traceback out of `run()` at the
            # caller, past a history the caller wanted, for the most ordinary
            # outcome there is: the model did not finish in time.
            if len(state["decisions"]) >= MAX_DECISIONS:
                state["status"] = "blocked"
                state["note"] = f"Reached the {MAX_DECISIONS}-decision budget without finishing."
                state["elapsed_ms"] = self._elapsed()
                return self.snapshot()
            if not self.desktop.fresh(state["page"]):
                state["page"] = self.desktop.observe()
            state["decision"] = None
            # A window that will not say what is in it gets photographed. This
            # is the expensive, less reliable path, so it is entered only when
            # the tree leaves nothing to choose from.
            capture = None
            if state["page"]["sparse"] and not self.pixels:
                state["note"] = (
                    "This window publishes almost nothing to the accessibility tree. "
                    "Pass activate=True to allow the screenshot fallback, which needs the app in front."
                )
            elif self.pixels and state["page"]["sparse"]:
                try:
                    capture = self.desktop.capture()
                except BridgeError as error:
                    state["capture_error"] = str(error)
            self.capture = capture
            decision = choose(state["page"], state["goal"], state["history"], capture=capture)
            state["decisions"].append(
                {
                    **{k: v for k, v in decision.items() if k != "offered"},
                    "fingerprint": state["page"]["fingerprint"],
                    "elapsed_ms": self._elapsed(),
                }
            )
            state["decision"] = decision
            # The guard runs on the operation that is about to execute, in the
            # same request that chose it: safety costs no extra round trip.
            risky = decision["risk"] >= self.risk_threshold and decision["operation"] not in {"DONE", "BLOCKED"}
            if risky and not (self.approve and self.approve(decision)):
                state["status"] = "needs_approval"
            else:
                state["status"] = "predicted"
            return self.snapshot()

        if name == "act":
            decision, page = state["decision"], state["page"]
            if not decision or body.get("fingerprint") != page["fingerprint"]:
                raise ValueError("Observe and choose before acting")
            if state["status"] == "needs_approval" and not body.get("approved"):
                raise ValueError(f"Held for approval: {decision['risk_reason']}")
            # Consume once, before any mutation. A retry cannot press twice.
            state["decision"] = None
            operation = decision["operation"]

            if operation in {"DONE", "BLOCKED"}:
                if not self.desktop.fresh(page):
                    state["status"] = "ready"
                    raise StaleWindow("The window changed since the decision. Choose again.")
                state["elapsed_ms"] = self._elapsed()
                if operation == "BLOCKED":
                    state["status"] = "blocked"
                    return self.snapshot()
                # DONE is the model's opinion about its own work. When the
                # caller supplied a way to check, the check decides.
                verdict = None
                if self.verify:
                    try:
                        verdict = self.verify(state["page"])
                    except Exception as error:
                        verdict = None
                        state["note"] = f"the outcome check itself failed: {error}"
                state["verified"] = verdict
                if verdict is False:
                    state["status"] = "blocked"
                    state["note"] = "The model reported DONE and the outcome check disagreed."
                else:
                    state["status"] = "done"
                return self.snapshot()

            if len(state["history"]) >= MAX_STEPS:
                state["status"] = "blocked"
                state["note"] = f"Stopped at the {MAX_STEPS}-operation budget without finishing."
                state["elapsed_ms"] = self._elapsed()
                return self.snapshot()

            action = decision["action"]
            text, helper = None, None
            if operation == "TYPE_KEYS":
                text = decision.get("text")
                if not text:
                    raise ValueError("TYPE_KEYS came back without text; nothing was typed.")
            elif operation == "TYPE_TEXT":
                if not self.desktop.fresh(page):
                    raise StaleWindow("The window changed before the value was settled. Choose again.")
                text = decision.get("text")
                if not text:
                    # A choice-only decision backend cannot write; ask a text model.
                    context = field_context(state["goal"], action, page, state["history"])
                    if self.pending_text and self.pending_text[0] == context:
                        _, text, helper = self.pending_text
                    else:
                        text, helper = field_text(context)
                        self.pending_text = (context, text, helper)

            if operation == "WAIT":
                time.sleep(0.15)
                result = {"detail": "waited", "mechanism": "sleep"}
            else:
                try:
                    result = self.desktop.act(action, page, text=text)
                except UnknownOutcome as error:
                    # Two failures share this branch, and both mean the window
                    # may not be what the decision was made against.
                    #
                    # The request was sent and its answer never came, so the
                    # operation may have run; or the bridge answered that it
                    # had already changed the window before it failed, which is
                    # what a TYPE_TEXT read-back failure is — the field was
                    # emptied on the way in. That second case used to arrive as
                    # an ordinary refusal and leave the step out of the history
                    # altogether, so the record said nothing had happened to a
                    # field that had been cleared.
                    #
                    # Either way: record the attempt, look at the window, and
                    # stop. Choosing again from here risks doing it twice, and
                    # some operations must not happen twice.
                    certain = isinstance(error, Executed)
                    state["elapsed_ms"] = self._elapsed()
                    state["history"].append(
                        {
                            "step": len(state["history"]) + 1,
                            "operation": operation,
                            "target": decision["target"],
                            "label": action.get("label", operation) if action else operation,
                            "text": text,
                            "outcome": "executed, unconfirmed" if certain else "unknown",
                            "detail": str(error),
                            "risk": decision["risk"],
                            "elapsed_ms": state["elapsed_ms"],
                        }
                    )
                    try:
                        state["page"] = self.desktop.observe()
                    except BridgeError:
                        pass
                    state["status"] = "blocked"
                    state["note"] = (
                        f"{operation} changed the window and could not be confirmed. "
                        f"Check the window before running anything else."
                        if certain
                        else f"{operation} may or may not have run. Check the window before running anything else."
                    )
                    return self.snapshot()
            self.pending_text = None
            state["elapsed_ms"] = self._elapsed()

            # Record execution before observing its result: a stale observation
            # must never erase an operation that actually ran.
            state["history"].append(
                {
                    "step": len(state["history"]) + 1,
                    "operation": operation,
                    "target": decision["target"],
                    "label": action.get("label", operation) if action else operation,
                    "text": text,
                    "mechanism": result.get("mechanism"),
                    "detail": result.get("detail"),
                    "took_focus": result.get("took_focus", False),
                    "verified_by_app": result.get("verified"),
                    "confidence": decision["confidence"],
                    "risk": decision["risk"],
                    "risk_reason": decision["risk_reason"],
                    "latency_ms": decision["latency_ms"],
                    "text_helper": helper["model"] if helper else None,
                    "usage": decision["usage"],
                    "window_changed": None,
                    "executed_ms": self._elapsed(),
                    "elapsed_ms": state["elapsed_ms"],
                }
            )
            before = page["fingerprint"]
            try:
                state["page"] = self.desktop.observe()
            except BridgeError as error:
                # The operation ran. If the window has gone — closed, or the app
                # terminated under it — the run stops, but what executed stays
                # on the record rather than disappearing with the exception.
                state["elapsed_ms"] = self._elapsed()
                state["history"][-1].update(window_changed=None, unobserved=str(error))
                state["status"] = "blocked"
                return self.snapshot()
            state["elapsed_ms"] = self._elapsed()
            if result.get("took_focus"):
                state["took_focus"] = True
            changed = state["page"]["fingerprint"] != before
            if decision.get("pixels") and self.capture:
                # The tree of a window like this hardly moves whatever happens
                # inside it, so the picture is what says whether the click
                # landed. A click that changed nothing visible missed.
                try:
                    after = self.desktop.capture()
                    difference = thumbnail_difference(self.capture.get("thumbnail"), after.get("thumbnail"))
                    if difference is not None:
                        changed = difference >= PIXEL_CHANGE_THRESHOLD
                        state["history"][-1]["pixel_difference"] = round(difference, 2)
                except BridgeError:
                    pass
            state["history"][-1].update(
                window_changed=changed,
                elapsed_ms=state["elapsed_ms"],
            )
            # Three operations that changed nothing visible is a stuck policy.
            recent = state["history"][-3:]
            stuck = len(recent) == 3 and all(
                h["window_changed"] is False and h["operation"] not in {"WAIT", "SCROLL_UP", "SCROLL_DOWN"}
                for h in recent
            )
            state["status"] = "blocked" if stuck else "ready"
            return self.snapshot()

        raise ValueError(f"Unknown command {name!r}")

    def run(self):
        """Yield one state per step. Stops at done, blocked, or a held operation."""
        while self.state["status"] not in {"done", "blocked", "needs_approval"}:
            yield self.command("tick")

    def approve_pending(self):
        """Release an operation that was held by the guard."""
        if self.state["status"] != "needs_approval":
            raise ValueError("Nothing is waiting for approval")
        self.state["status"] = "predicted"
        return self.command("act", {"fingerprint": self.state["page"]["fingerprint"], "approved": True})

    def close(self):
        self.desktop.close()

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()
