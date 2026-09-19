#!/usr/bin/env python3
"""Run one goal against one app.

    uv run --env-file .env python examples/run.py \
      --app TextEdit --goal 'Replace the text with a haiku about the menu bar.'

Add --activate when the goal needs menu-bar commands: an inactive app reports
every menu item as disabled. Without it nothing takes the screen from you.
"""

import argparse

from open_computer_use import Agent


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--app", required=True, help="app name, bundle id, or pid")
    parser.add_argument("--goal", required=True)
    parser.add_argument("--activate", action="store_true", help="bring the app to the front")
    parser.add_argument("--yes", action="store_true", help="approve consequential operations automatically")
    parser.add_argument("--risk-threshold", type=float, default=0.5)
    options = parser.parse_args()

    approve = None
    if options.yes:
        def approve(decision):
            print(f"   approved: {decision['risk_reason']} (risk {decision['risk']:.2f})")
            return True

    with Agent(
        options.app,
        options.goal,
        approve=approve,
        activate=options.activate,
        risk_threshold=options.risk_threshold,
    ) as agent:
        shown = 0
        for state in agent.run():
            # A tick that only decided (DONE, or a stale retry) adds no step.
            for step in state["history"][shown:]:
                value = f' "{step["text"]}"' if step["text"] else ""
                if step.get("outcome"):
                    # A step that did not run cleanly says so, and says why. It
                    # used to say nothing at all, because a refused operation
                    # raised out of the run and took the printed history with it.
                    note = f'   [{step["outcome"]}: {step.get("detail", "")}]'
                elif step["window_changed"]:
                    note = ""
                else:
                    note = "   (nothing changed)"
                print(f'{step["elapsed_ms"]:>6} ms  {step["operation"]:<12} {step["label"]}{value}{note}')
            shown = len(state["history"])
        final = agent.state
        print(f'\n{final["status"]} after {len(final["history"])} operations, {final["elapsed_ms"]} ms')
        if final["status"] == "needs_approval":
            decision = final["decision"]
            print(f'held: {decision["operation"]} — {decision["risk_reason"]} (risk {decision["risk"]:.2f})')
            print("re-run with --yes to let it through, or raise --risk-threshold")
        if final["status"] == "done":
            print("DONE is the model's claim. Check the window yourself before believing it.")


if __name__ == "__main__":
    main()
