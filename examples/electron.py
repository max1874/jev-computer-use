#!/usr/bin/env python3
"""The same window, read twice, with one number changed.

The most useful thing this project learned is also the easiest to get wrong,
because the wrong answer comes with real measurements attached. Read a Chromium
window — Lark, Feishu, Slack, VS Code, anything Electron — with the traversal
limit that suits native apps, and it reports a handful of elements, almost no
text and nowhere to type. The obvious conclusion is that Electron keeps its
interface out of the accessibility tree and only a screenshot will do.

It is not true. The tree is there. Chromium puts the web area about nine levels
below the window and the interface another ten to twenty below that, so a walk
that stops at eighteen ends in the empty scaffolding above the content and
reports, accurately, that it found nothing.

This reads one window at several depths and prints what each one sees. Nothing
is pressed, nothing is typed, nothing is brought to the front, and no model is
called — it only looks.

    uv run python examples/electron.py                 # Lark
    uv run python examples/electron.py --app Slack
    uv run python examples/electron.py --app Finder    # a native window, flat

A native window is the control: it puts everything within a dozen levels, so
its numbers do not move and the deeper walk costs it nothing.
"""

import argparse
import time

from open_computer_use.desktop import Bridge, sparseness

DEPTHS = [18, 26, 40, 60]


def look(bridge, app, depth, limit):
    """One snapshot, and what it would let an agent do."""
    started = time.perf_counter()
    page = bridge.call("snapshot", app=app, menus=False, limit=limit, depth=depth, timeout=60)
    elapsed = round((time.perf_counter() - started) * 1000)
    page.update(sparseness(page))
    return {
        "depth": depth,
        "ms": elapsed,
        "elements": len(page["elements"]),
        "named": page["named"],
        "coverage": page["coverage"],
        "text": len(page["text"]),
        "typeable": len([e for e in page["elements"] if "TYPE_TEXT" in e["operations"]]),
        "sparse": page["sparse"],
        "page": page,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--app", default="Lark", help="app name, bundle id, or pid")
    parser.add_argument("--limit", type=int, default=600)
    options = parser.parse_args()

    bridge = Bridge()
    try:
        status = bridge.call("status")
        if not status["accessibility"]:
            raise SystemExit(
                "Accessibility permission is missing. System Settings > Privacy & Security > "
                "Accessibility: enable the app running this shell, then restart it."
            )
        try:
            bridge.call("snapshot", app=options.app, menus=False, limit=10, depth=8)
        except Exception as error:
            raise SystemExit(f"Cannot read {options.app!r}: {error}") from None

        rows = [look(bridge, options.app, depth, options.limit) for depth in DEPTHS]

        print(f"\n{options.app} — the same window, read at four traversal depths\n")
        print(f'{"depth":>6}  {"elements":>8}  {"named":>5}  {"coverage":>8}  {"text":>6}  '
              f'{"typeable":>8}  {"sparse?":>7}  {"ms":>5}')
        for row in rows:
            print(f'{row["depth"]:>6}  {row["elements"]:>8}  {row["named"]:>5}  {row["coverage"]:>8}  '
                  f'{row["text"]:>6}  {row["typeable"]:>8}  {str(row["sparse"]):>7}  {row["ms"]:>5}')

        first, last = rows[0], rows[-1]
        print()
        gained = last["text"] - first["text"]
        if gained > 200 or last["typeable"] > first["typeable"]:
            print(f'At depth {first["depth"]}: {first["text"]} characters of readable text, '
                  f'{first["typeable"]} places to type, {first["named"]} named elements.')
            print(f'At depth {last["depth"]}: {last["text"]}, {last["typeable"]}, {last["named"]}. '
                  f'Same window, same instant, one number changed.')
            # The sparseness measure is a separate question from whether the
            # walk reached anything, and on this window it can be wrong in the
            # comfortable direction: a single element covering the whole window
            # keeps the coverage score high while the walk sees nothing at all.
            if not first["sparse"]:
                print(f'\nNote that the shallow read does not measure as sparse — '
                      f'coverage {first["coverage"]} is carried by one element spanning the window. '
                      f'It would not have triggered the screenshot fallback either. It was simply '
                      f'an empty table that nothing flagged.')
        elif last["text"] < 50 and last["typeable"] == 0:
            print("This window publishes almost nothing at any depth. For a window like this the "
                  "screenshot fallback is the only way in.")
        else:
            print(f'Nothing was hiding: this window reads the same at depth {first["depth"]} '
                  f'as at depth {last["depth"]}. That is what a native window looks like.')

        # What the deeper walk actually put within reach.
        named = [e for e in last["page"]["elements"] if e["label"] and e["operations"]]
        if named:
            print(f'\nNamed and actionable at depth {last["depth"]} ({len(named)} of them):')
            for element in named[:12]:
                label = element["label"].replace("\n", " ")
                print(f'  {element["role"].removeprefix("AX"):<12} {label[:66]}')
    finally:
        bridge.close()


if __name__ == "__main__":
    main()
