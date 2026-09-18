#!/usr/bin/env python3
"""Exercise the accessibility bridge against a real app, with no model calls.

Everything here runs against a TextEdit window that is opened in the
background and never brought to the front: reading the element table, typing
through the accessibility value, and checking that the staleness guards refuse.
Your own window keeps the focus the whole time.

    python3 scripts/check_bridge.py              # background only
    python3 scripts/check_bridge.py --foreground # also check the menu bar

The menu checks need the app active, because an inactive app reports every
menu command as disabled, so they are opt-in.
"""

import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jev_computer_use.desktop import BridgeError, Desktop, StaleWindow  # noqa: E402

PASS, FAIL = "  ok  ", " FAIL "
failures = []


def check(name, condition, detail=""):
    print(f"[{PASS if condition else FAIL}] {name}" + (f" — {detail}" if detail else ""))
    if not condition:
        failures.append(name)


def main():
    foreground = "--foreground" in sys.argv
    scratch = Path(tempfile.mkdtemp(prefix="jev-cu-")) / "bridge-check.txt"
    scratch.write_text("before\n")
    # -g opens the document without taking the screen from whoever has it.
    subprocess.run(["open", "-g", "-a", "TextEdit", str(scratch)], check=True)
    time.sleep(1.5)

    desktop = Desktop("TextEdit", activate=foreground)
    try:
        page = desktop.observe()
        check("the window was read without taking the front", foreground or not page["active"])
        check("snapshot returns the scratch window", page["window"] == scratch.name, page["window"])
        check("elements are indexed from 1", [e["index"] for e in page["elements"]][:1] == ["1"])

        fields = [e for e in page["elements"] if "TYPE_TEXT" in e["operations"]]
        check("the text area offers TYPE_TEXT", bool(fields), f"{len(fields)} editable")
        buttons = [e for e in page["elements"] if "PRESS" in e["operations"]]
        check("window buttons are named, not blank", all(e["label"] for e in buttons), f"{len(buttons)} buttons")
        check("checkboxes are never offered as editable fields", all(e["role"] != "AXCheckBox" for e in fields))

        field = fields[0]
        written = "typed through the accessibility value, no keystrokes, no focus"
        result = desktop.act({"operation": "TYPE_TEXT", "op": "TYPE_TEXT", "path": field["path"]}, page, text=written)
        check(
            "TYPE_TEXT reports the mechanism it used",
            result["mechanism"] in {"AXValue", "CGEvent→pid"},
            result["mechanism"],
        )

        after = desktop.observe()
        check("the window state actually changed", after["fingerprint"] != page["fingerprint"])
        check("the typed value is in the tree", written in after["elements"][0]["value"])
        check("the earlier observation is now stale", not desktop.fresh(page))
        check("the current observation is fresh", desktop.fresh(after))
        check("the app never came to the front", foreground or not after["active"])

        try:
            desktop.act(
                {"operation": "PRESS", "op": "PRESS", "path": "0.1", "expect": "a label that is not there"},
                after,
            )
            check("a stale expectation is refused", False, "the press went through")
        except StaleWindow as error:
            check("a stale expectation is refused", True, str(error)[:60])

        try:
            desktop.act({"operation": "PRESS", "op": "PRESS", "path": "0.99.99"}, after)
            check("an impossible path is refused", False, "the press went through")
        except (StaleWindow, BridgeError) as error:
            check("an impossible path is refused", True, str(error)[:60])

        check(
            "an inactive app offers no menu commands",
            foreground or not after["menus"],
            "menu validation needs the app active",
        )

        if foreground:
            menus = desktop.observe()
            check("menu commands are enumerated while the menus are closed", len(menus["menus"]) > 5,
                  f"{len(menus['menus'])} commands")
            check("the Services submenu is left out", not any(">Services>" in m["path"] for m in menus["menus"]))
            check("Make Rich Text is offered", any(m["path"] == "Format>Make Rich Text" for m in menus["menus"]))
            desktop.act({"operation": "MENU", "op": "MENU", "menu": "Format>Make Rich Text"}, menus)
            rich = desktop.observe()
            check("a menu command runs without opening its menu", rich["fingerprint"] != menus["fingerprint"])
            desktop.act({"operation": "MENU", "op": "MENU", "menu": "Edit>Undo Make Rich Text"}, rich)
            try:
                desktop.act({"operation": "MENU", "op": "MENU", "menu": "Format>No Such Command"}, rich)
                check("an unknown menu command is refused", False, "it ran")
            except BridgeError as error:
                check("an unknown menu command is refused", True, str(error)[:60])

        started = time.perf_counter()
        for _ in range(10):
            desktop.observe()
        per_call = (time.perf_counter() - started) * 100
        check("a snapshot stays under 150 ms", per_call < 150, f"{per_call:.0f} ms per snapshot")
    finally:
        # Leave the machine as it was found.
        desktop.close()
        subprocess.run(
            ["osascript", "-e", 'tell application "TextEdit" to close every document without saving'],
            capture_output=True,
        )
        subprocess.run(["osascript", "-e", 'tell application "TextEdit" to quit'], capture_output=True)
        scratch.unlink(missing_ok=True)

    print()
    if failures:
        print(f"{len(failures)} check(s) failed: " + ", ".join(failures))
        return 1
    print("all bridge checks passed" + ("" if foreground else "  (menu checks skipped; pass --foreground)"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
