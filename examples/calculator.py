#!/usr/bin/env python3
"""Six presses in a window that stays behind everything else.

Calculator is a good honest test: two dozen elements, every one of them a
plain button with a real label, no text to generate, and a result that is
either right or wrong. The agent has to pick 1, 2, ×, 3, 4, = in that order
and then recognise that it is finished.

    uv run --env-file .env python examples/calculator.py

Nothing is brought to the front. Check with `background=True` in the output,
or just keep working while it runs.
"""

import argparse
import subprocess
import time

from open_computer_use import Agent
from open_computer_use.desktop import Desktop

GOAL = "Compute 12 times 34 and leave the result on the display."
EXPECTED = "408"


def clear(app):
    """Start from a known display, and from a window that only shows this run.

    Calculator keeps a visible tape of earlier calculations, and All Clear does
    not clear it. Left alone it accumulates across runs until the window is
    showing the expected answer before the agent has pressed anything — which
    is the state this example is supposed to be proving the absence of. The
    button is 'All Clear' only when the display is already clear.
    """
    desktop = Desktop(app, menus=True)
    try:
        page = desktop.observe()
        if any(m["label"] == "View > Hide History" for m in page.get("menus", [])):
            desktop.act({"operation": "MENU", "op": "MENU", "menu": "View > Hide History"}, page)
            page = desktop.observe()
        button = next((e for e in page["elements"] if e["label"] in ("All Clear", "Clear")), None)
        if button:
            desktop.act({"operation": "PRESS", "op": "PRESS", "path": button["path"], "expect": "Clear"}, page)
    finally:
        desktop.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--goal", default=GOAL)
    parser.add_argument("--expect", default=EXPECTED)
    options = parser.parse_args()

    # macOS terminates an idle background app on its own, so make sure it is up.
    subprocess.run(["open", "-g", "-a", "Calculator"], check=True)
    time.sleep(2.0)
    clear("Calculator")

    # The loop asks this before accepting DONE, so the model cannot declare
    # victory over a display that does not say what it should. Only the last
    # line counts: anywhere in the window is not the same as on the display,
    # and a check that accepts anywhere is one a leftover from the previous
    # run can satisfy without this run doing anything at all.
    def reached(page):
        lines = [line for line in page["text"].replace("‎", "").splitlines() if line.strip()]
        return bool(lines) and options.expect in lines[-1]

    started = time.perf_counter()
    with Agent("Calculator", options.goal, verify=reached) as agent:
        shown = 0
        for state in agent.run():
            for step in state["history"][shown:]:
                print(
                    f'{step["elapsed_ms"]:>6} ms  {step["operation"]:<6} {step["label"]:<14}'
                    f'  model {step["latency_ms"]:>4} ms   risk {step["risk"]:.2f}'
                )
            shown = len(state["history"])
        final = agent.state
    total = round((time.perf_counter() - started) * 1000)

    decisions = [d["latency_ms"] for d in final["decisions"]]
    model_ms = sum(decisions)
    print(f'\n{final["status"]} after {len(final["history"])} operations, {total} ms')
    print(f"  {len(decisions)} model calls, {model_ms} ms of it waiting on the model "
          f"({model_ms / total * 100:.0f}% of the wall clock)")
    print(f'  outcome check: {final["verified"]}   took the screen: {final["took_focus"]}')

    # Independent verification: read the display, not the model's DONE.
    desktop = Desktop("Calculator")
    try:
        page = desktop.observe()
        display = page["text"].replace("‎", "")
        print("\nindependent check")
        print(f'  display                         : {display!r}')
        print(f'  contains {options.expect:<22}: {options.expect in display}')
        print(f'  the app never came to the front : {not page["active"]}')
    finally:
        desktop.close()


if __name__ == "__main__":
    main()
