#!/usr/bin/env python3
"""The reproducible demo: fill a scratch document in a window that never comes forward.

Opens a temporary file in TextEdit with `open -g`, so the app draws its window
without taking the screen from whatever you are doing. The agent then works on
that window through the accessibility tree while you keep typing somewhere else.

    uv run --env-file .env python examples/textedit.py

The check at the end is independent of the model: it reads the document back out
of the accessibility tree and compares it against what was asked for. A DONE
choice is not evidence.
"""

import argparse
import subprocess
import tempfile
import time
from pathlib import Path

from jev_computer_use import Agent
from jev_computer_use.desktop import Desktop

SENTENCE = "The quick brown fox jumps over the lazy dog."
GOAL = f"Replace everything in the document with exactly this one sentence: '{SENTENCE}'"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--keep-open", action="store_true", help="leave TextEdit running afterwards")
    options = parser.parse_args()

    scratch = Path(tempfile.mkdtemp(prefix="jev-cu-")) / "demo.txt"
    scratch.write_text("this line should be replaced\n")
    subprocess.run(["open", "-g", "-a", "TextEdit", str(scratch)], check=True)
    time.sleep(1.5)

    started = time.perf_counter()
    with Agent("TextEdit", GOAL) as agent:
        shown = 0
        for state in agent.run():
            # A tick that only decided (DONE, or a stale retry) adds no step.
            for step in state["history"][shown:]:
                value = f' "{step["text"]}"' if step["text"] else ""
                print(f'{step["elapsed_ms"]:>6} ms  {step["operation"]:<12} {step["label"]}{value}')
            shown = len(state["history"])
        final = dict(agent.state)
    elapsed = round((time.perf_counter() - started) * 1000)
    print(f'\n{final["status"]} after {len(final["history"])} operations, {elapsed} ms')

    # Independent verification: read the window again, from a fresh connection.
    desktop = Desktop("TextEdit")
    try:
        page = desktop.observe()
        body = next((e["value"] for e in page["elements"] if e["role"] == "AXTextArea"), "")
        never_fronted = not page["active"]
    finally:
        desktop.close()

    print("\nindependent check")
    print(f'  the sentence is in the document : {SENTENCE.strip(".") in body}')
    print(f'  the old line is gone            : {"this line should be replaced" not in body}')
    print(f'  the app never came to the front : {never_fronted}')
    print(f'  document now                    : {body[:120]!r}')

    if not options.keep_open:
        subprocess.run(
            ["osascript", "-e", 'tell application "TextEdit" to close every document without saving',
             "-e", 'tell application "TextEdit" to quit'],
            capture_output=True,
        )
        scratch.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
