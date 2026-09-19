"""The accessibility bridge: one snapshot in, one guarded operation out.

The Swift helper stays alive for the whole session, so a step costs one
accessibility traversal rather than a process launch.
"""

import json
import os
import selectors
import subprocess
import threading
import time
from math import isqrt
from pathlib import Path

ROOT = Path(__file__).resolve().parent
BUILD = ROOT.parent / "scripts" / "build.sh"


class StaleWindow(Exception):
    """The window changed between the decision and its execution."""


class BridgeError(RuntimeError):
    """The bridge refused an operation. Nothing was executed."""


class UnknownOutcome(RuntimeError):
    """The request was sent and the window may already have changed.

    These must not be retried. The operation may have run: a press that reached
    the app and then lost its reply looks exactly like a press that never
    arrived. Observe the window and decide from what is there, rather than
    sending it again.
    """


class Executed(UnknownOutcome):
    """The operation ran, changed the window, and could not be confirmed.

    Not a variety of "nothing happened". `TYPE_TEXT` empties the field before
    writing to it, so a read-back that does not match is a failure reported
    about a field this call has already emptied. Treating that as a refusal
    left the step out of the agent's history entirely and offered the model a
    fresh choice over a field it had itself cleared, with no record that
    anything had been done to it. A subclass, so every caller that already
    knows not to retry an uncertain outcome covers this one too.
    """


def binary():
    """Build the bridge if needed and return its path."""
    override = os.environ.get("AXBRIDGE_BIN")
    if override:
        return override
    result = subprocess.run([str(BUILD)], capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"could not build the accessibility bridge:\n{result.stderr.strip()}")
    return result.stdout.strip()


class Bridge:
    """A line-delimited JSON-RPC pipe to the Swift helper."""

    def __init__(self):
        self.process = subprocess.Popen(
            [binary(), "serve"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        self.lock = threading.Lock()
        self.counter = 0
        self.selector = selectors.DefaultSelector()
        self.selector.register(self.process.stdout, selectors.EVENT_READ)

    def _readline(self, timeout):
        """One line, or None if it did not arrive in time."""
        if self.selector.select(timeout):
            return self.process.stdout.readline()
        return None

    def call(self, method, timeout=20, mutating=False, **body):
        with self.lock:
            if self.process.poll() is not None:
                raise BridgeError("the accessibility bridge exited before the request was sent")
            self.counter += 1
            request = {"id": self.counter, "method": method, **body}
            try:
                self.process.stdin.write(json.dumps(request) + "\n")
                self.process.stdin.flush()
            except (OSError, ValueError) as error:
                # Never left this process, so nothing can have happened.
                raise BridgeError(f"the request was not sent: {error}") from None
            # Past this line the operation may have run, whatever comes back.
            line = self._readline(timeout)

        if line is None or line == "":
            lost = "timed out" if line is None else "the bridge closed the pipe"
            if mutating:
                raise UnknownOutcome(
                    f"{method} was sent and {lost} after {timeout}s. It may have run. "
                    "Observe the window rather than sending it again."
                )
            raise BridgeError(f"{method} {lost} after {timeout}s")
        answer = json.loads(line)
        if answer.get("id") != request["id"]:
            # The pipe is one request at a time; a mismatch means it desynced.
            raise UnknownOutcome(
                f"expected a reply to {request['id']} and got {answer.get('id')}; the bridge is out of step"
            )
        if not answer.get("ok"):
            message = answer.get("error", "unknown bridge error")
            # Most bridge failures are pre-flight — a path that no longer
            # resolves, a disabled element, a guard that refused — and nothing
            # ran. The bridge says when that is not true, and it is not true
            # for the one operation that clears a field before filling it.
            if answer.get("acted"):
                raise Executed(message)
            raise BridgeError(message)
        return answer["result"]

    def close(self):
        try:
            self.selector.close()
        except Exception:
            pass
        if self.process.poll() is None:
            try:
                self.process.stdin.close()
            except OSError:
                pass
            try:
                self.process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.process.kill()


# How long to let the interface settle before the next observation. A menu or a
# sheet animates in; paying for a decision before it exists wastes a round trip.
# TYPE_TEXT needs almost nothing, because setting the accessibility value is
# synchronous and `act` has already read the value back before returning.
SETTLE_MS = {
    "MENU": 220,
    "SELECT": 220,
    "PRESS": 60,
    # Stepping a control is the app changing its own value, done immediately.
    "INCREMENT": 60,
    "DECREMENT": 60,
    "TYPE_TEXT": 25,
    "WAIT": 150,
    # A web interface answers a click by re-rendering, which is slower than a
    # native control flipping state, and nothing here can read that it finished.
    "CLICK_POINT": 400,
    # Same delivery as CLICK_POINT — a real pointer, a real event — so the app
    # answers on its own schedule rather than returning when the call does.
    "CLICK": 400,
    "TYPE_KEYS": 250,
}


# A region is a quarter of the window's width and height, so a sixteenth of its
# area, and they overlap by half so that a change straddling a boundary still
# falls whole inside some region.
THUMBNAIL_BLOCK = 4
THUMBNAIL_STRIDE = 2


def thumbnail_difference(before, after):
    """The biggest change in any one part of the window, 0 to 255.

    Averaging the whole window was the obvious way to do this and it does not
    work, because the thing being measured is local and the average is not. A
    real change divided by an unchanged majority comes out near zero: pressing
    a digit in Calculator, which is as unambiguous as an interface gets, scores
    0.30 against a threshold of 2. A block of a real Lark window 341 points on
    a side — a third of its width — scores 1.88 and is also read as nothing.
    Three of those in a row and `Agent` calls the run stuck and stops it.

    So the window is scored in overlapping regions and the loudest one wins. A
    change confined to a sixteenth of the window now counts sixteen times for
    what it is, and a change larger than that still reads as large, because the
    regions overlap and one of them lies inside it.

    Measured on the pairs this has to separate. Nothing happening: seven real
    windows captured twice back to back score 0.00 to 0.19, and Calculator's
    All Clear pressed against an already-clear display scores exactly 0.00.
    Something happening: one digit 2.50, All Clear against a display holding
    something 2.25 to 4.62 depending on what it held, an 85-point square at
    reading contrast 7.50. Repeated presses give the same score to two decimals.

    The hover highlight under the pointer was the one case arithmetic put near
    the threshold, and it does not arise. `clickImagePoint` moves the pointer
    back before it returns, and the second capture is taken after that and
    after the settle, so the highlight is gone by the time anything is
    compared. Measured rather than assumed: four real clicks on empty space in
    a Linear window — pointer really moved there, button really pressed —
    score 0.00, and clicking a tab in that same window scores 2.50. The
    whole-window average scores that same tab click 0.41 and calls it nothing.
    """
    if not before or not after or len(before) != len(after):
        return None
    side = isqrt(len(before))
    if side * side != len(before) or side < THUMBNAIL_BLOCK:
        # Not a square reduction; fall back to the plain average rather than
        # inventing a geometry the caller never promised.
        return sum(abs(a - b) for a, b in zip(before, after)) / len(before)
    delta = [abs(a - b) for a, b in zip(before, after)]
    cells = THUMBNAIL_BLOCK * THUMBNAIL_BLOCK
    corners = range(0, side - THUMBNAIL_BLOCK + 1, THUMBNAIL_STRIDE)
    return max(
        sum(
            delta[row * side + column]
            for row in range(top, top + THUMBNAIL_BLOCK)
            for column in range(left, left + THUMBNAIL_BLOCK)
        )
        / cells
        for top in corners
        for left in corners
    )


# Below this the window looks unchanged. Nothing happening measures 0.00 to
# 0.19 and the weakest real change measured 2.25, so the line goes in the
# middle of that rather than against either edge — and nearer the quiet end,
# because the two mistakes do not cost the same. A missed change is counted
# towards the three that make `Agent` call the run stuck and stop it, so three
# of them end the task. A change that was really nothing costs one more step
# of a budget that already has a ceiling.
#
# The same control in a larger window is a smaller share of its region and so
# scores lower, and a window large enough to need this path is the large case
# by definition. That is the other reason not to sit on top of 2.25.
PIXEL_CHANGE_THRESHOLD = 1.0


# A window is sparse when the tree accounts for almost none of its area AND
# reads back almost no text AND offers nowhere to type. Measured across real
# windows: Linear 0.00 coverage / 0 characters — against Feishu 0.05 / 2431,
# Lark 1.00 / 3399, TextEdit 0.15 / 659, Finder 0.11 / 1004, Calculator
# 0.55 / 13. Every one of the three has to agree, because each is wrong on its
# own: coverage alone would condemn TextEdit and Finder, whose one big text
# area covers little but says plenty, and Feishu, which covers 5% of itself and
# still hands over every message in the room; text alone would condemn
# Calculator, which has nothing to say and everything to press.
COVERAGE_LIMIT = 0.30
TEXT_LIMIT = 200


def sparseness(page):
    """Does this window publish enough of itself to be driven by the tree alone?

    Far fewer windows than expected. An Electron app renders its interface into
    a web content area, and it is tempting to conclude that the area is closed
    to the accessibility tree, because that is exactly what a shallow walk sees:
    the native chrome — sidebar, toolbar, window buttons, all properly named —
    wrapped around a silent hole. The hole is an artefact of the walk. Chromium
    puts the web area nine levels below the window and the interface inside it
    another ten to twenty below that, so a limit set for native windows stops in
    the empty scaffolding above the content. See DEFAULT_MAX_DEPTH in the bridge.

    What is left after fixing that is the genuine article: Linear publishes
    three elements, no text and nothing to press at any depth. For a window like
    that the tree has nothing to offer and the screenshot is the only way in.
    """
    frame = page.get("window_frame") or [0, 0, 0, 0]
    area = max(1, frame[2] * frame[3])
    # Nested elements double-count, which only makes a window look better
    # covered than it is — never worse, so it cannot create a false positive.
    coverage = min(1.0, sum(e["frame"][2] * e["frame"][3] for e in page["elements"]) / area)
    actionable = [e for e in page["elements"] if e["operations"]]
    editable = [e for e in page["elements"] if "TYPE_TEXT" in e["operations"]]
    sparse = not actionable or (
        coverage < COVERAGE_LIMIT and len(page["text"]) < TEXT_LIMIT and not editable
    )
    return {
        "sparse": sparse,
        "actionable": len(actionable),
        "named": len([e for e in actionable if e["label"]]),
        "coverage": round(coverage, 3),
    }


class Desktop:
    """One app, observed as an indexed element table and driven by path."""

    def __init__(self, app, *, menus=True, limit=250, activate=False):
        self.bridge = Bridge()
        status = self.bridge.call("status")
        if not status["accessibility"]:
            self.close()
            raise RuntimeError(
                "Accessibility permission is missing. System Settings > Privacy & Security > "
                "Accessibility: enable the app running this shell, then restart it."
            )
        if status["locked"]:
            self.close()
            raise RuntimeError("The screen is locked; the accessibility tree is empty until it is unlocked.")
        self.app = app
        self.menus = menus
        self.limit = limit
        if activate:
            self.bridge.call("activate", app=app)

    def observe(self):
        page = self.bridge.call("snapshot", app=self.app, menus=self.menus, limit=self.limit)
        page["observed_at"] = time.time()
        page.update(sparseness(page))
        return page

    def capture(self, width=1800, quality=0.6):
        """A picture of the window, in a coordinate space a click can be named in.

        Width and quality were varied separately, on one 2560-point Linear
        window, against one tab whose position had been confirmed by clicking
        it, with `deepseek-v4.1-flash` naming the point five times per cell:

            1000 wide, quality 0.6   median 72px off, 0 of 5 within 15px
            1000 wide, quality 0.85  median 72px off, 0 of 5 within 15px
            1800 wide, quality 0.6   median  3px off, 5 of 5 within 15px
            1800 wide, quality 0.85  median  3px off, 5 of 5 within 15px

        So width is what mattered here and quality changed nothing, which is
        why quality stays where it was and only the width moved. A 44 point tab
        is 17 pixels across at 1000 and 31 at 1800. One window, one target, one
        model: enough to set a default, not enough to call it a general rule.

        The wider picture costs about 40% more prompt tokens on a window that
        large — 1586 against 2225 — and nothing at all on a small one, because
        the bridge never scales a capture up. 2200 wide billed the same 2226
        tokens as 1800, which is a reason not to pay for more pixels here and
        not evidence about what the model does or does not look at.
        """
        return self.bridge.call("capture", app=self.app, width=width, quality=quality)

    def fresh(self, page):
        """Has the window's semantic state survived since this observation?"""
        try:
            now = self.bridge.call("fingerprint", app=self.app, limit=self.limit)
        except BridgeError:
            return False
        return now["fingerprint"] == page["fingerprint"]

    def act(self, action, page, text=None):
        """Execute one chosen action. A refused guard raises; nothing runs twice."""
        operation = action["operation"]
        request = {"app": self.app, "op": action.get("op", operation)}
        if "path" in action:
            request["path"] = action["path"]
            # The element at that path must still be the one that was chosen.
            for guard in ("expect", "expect_role", "expect_id"):
                if action.get(guard):
                    request[guard] = action[guard]
        if "menu" in action:
            request["menu"] = action["menu"]
        if "option" in action:
            request["option"] = action["option"]
        if "key" in action:
            request["key"] = action["key"]
        for field in ("x", "y", "scale", "window_id", "window_size"):
            if field in action:
                request[field] = action[field]
        if text is not None:
            request["text"] = text
        try:
            result = self.bridge.call("act", mutating=True, timeout=30, **request)
        except BridgeError as error:
            if str(error).startswith("stale:") or "is stale" in str(error):
                raise StaleWindow(str(error)) from None
            raise
        settle = SETTLE_MS.get(operation, 60)
        time.sleep(settle / 1000)
        return result

    def close(self):
        self.bridge.close()
