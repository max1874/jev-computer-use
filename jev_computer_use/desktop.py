"""The accessibility bridge: one snapshot in, one guarded operation out.

The Swift helper stays alive for the whole session, so a step costs one
accessibility traversal rather than a process launch.
"""

import json
import os
import subprocess
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
BUILD = ROOT.parent / "scripts" / "build.sh"


class StaleWindow(Exception):
    """The window changed between the decision and its execution."""


class BridgeError(RuntimeError):
    """The bridge refused an operation. Nothing was executed."""


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

    def call(self, method, **body):
        with self.lock:
            if self.process.poll() is not None:
                raise BridgeError("the accessibility bridge exited")
            self.counter += 1
            request = {"id": self.counter, "method": method, **body}
            self.process.stdin.write(json.dumps(request) + "\n")
            self.process.stdin.flush()
            line = self.process.stdout.readline()
        if not line:
            raise BridgeError("the accessibility bridge stopped responding")
        answer = json.loads(line)
        if not answer.get("ok"):
            raise BridgeError(answer.get("error", "unknown bridge error"))
        return answer["result"]

    def close(self):
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
    "TYPE_TEXT": 25,
    "WAIT": 150,
    # A web interface answers a click by re-rendering, which is slower than a
    # native control flipping state, and nothing here can read that it finished.
    "CLICK_POINT": 400,
    "TYPE_KEYS": 250,
}


def thumbnail_difference(before, after):
    """Mean absolute difference between two 16x16 greyscale reductions, 0 to 255."""
    if not before or not after or len(before) != len(after):
        return None
    return sum(abs(a - b) for a, b in zip(before, after)) / len(before)


# Below this the window looks unchanged: a caret blink and a hover highlight
# land under it, a menu or panel opening lands well above.
PIXEL_CHANGE_THRESHOLD = 2.0


def sparseness(page):
    """Does this window publish enough of itself to be driven by the tree alone?

    A Chromium-based app (Electron: Lark, Slack, VS Code, Discord) renders its
    real interface into the web content area and, unless something has switched
    its accessibility support on, exposes that area as a pile of nameless
    groups. The window still has elements — they just do not say what they are.

    The test is that shape specifically: many actionable elements, most of them
    unnamed. A small window whose few controls are all named is not sparse, and
    a window with no actionable elements at all certainly is.
    """
    actionable = [e for e in page["elements"] if e["operations"]]
    named = [e for e in actionable if e["label"]]
    unnamed = len(actionable) - len(named)
    sparse = not actionable or (len(named) / len(actionable) < 0.5 and unnamed >= 8)
    return {
        "sparse": sparse,
        "actionable": len(actionable),
        "named": len(named),
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

    def capture(self, width=1000, quality=0.6):
        """A picture of the window, in a coordinate space a click can be named in."""
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
            if action.get("expect"):
                request["expect"] = action["expect"]
        if "menu" in action:
            request["menu"] = action["menu"]
        if "option" in action:
            request["option"] = action["option"]
        if "key" in action:
            request["key"] = action["key"]
        for field in ("x", "y", "scale"):
            if field in action:
                request[field] = action[field]
        if text is not None:
            request["text"] = text
        try:
            result = self.bridge.call("act", **request)
        except BridgeError as error:
            if str(error).startswith("stale:") or "is stale" in str(error):
                raise StaleWindow(str(error)) from None
            raise
        settle = SETTLE_MS.get(operation, 60)
        time.sleep(settle / 1000)
        return result

    def close(self):
        self.bridge.close()
