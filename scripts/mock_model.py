#!/usr/bin/env python3
"""A loopback stand-in for the decision model, so the loop can be exercised offline.

It speaks just enough of the OpenAI chat-completions shape to answer the
request the agent actually sends: it reads the response schema, fills every
head with a valid choice, and picks the operation from the state by a rule a
model would learn. It has no intelligence and proves none — it proves the
transport, the schema, the target heads, the guard, and the execution path.

    python3 scripts/mock_model.py --port 8799
"""

import argparse
import json
from http.server import BaseHTTPRequestHandler, HTTPServer


RISK = 0.0


def decide(state, properties):
    """Fill every head with a valid choice, then pick the operation."""
    answer = {}
    for name, spec in properties.items():
        if "enum" in spec:
            answer[name] = spec["enum"][0]
    answer["confidence"] = 0.9
    answer["risk"] = RISK
    answer["risk_reason"] = "reversible interface state" if RISK < 0.5 else "overwrites a saved document"
    if "type_text_value" in properties:
        answer["type_text_value"] = None

    offered = set(properties["operation"]["enum"])
    done = [action for action in state.get("recent_actions", []) if action.get("operation") == "TYPE_TEXT"]
    goal = state.get("goal", "")
    if goal.count("'") >= 2:
        quoted = goal.split("'")[1]
    elif ": " in goal:
        quoted = goal.split(": ", 1)[1].split("  ")[0].strip()
    else:
        quoted = goal

    if not done and "TYPE_TEXT" in offered and "type_text_target" in properties:
        answer["operation"] = "TYPE_TEXT"
        # The largest editable element: the document body rather than a toolbar field.
        editable = [e for e in state["elements"] if "TYPE_TEXT" in e["operations"]]
        answer["type_text_target"] = editable[0]["index"] if editable else properties["type_text_target"]["enum"][0]
        answer["type_text_value"] = quoted
    else:
        answer["operation"] = "DONE"
    return answer


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        state = json.loads(body["messages"][-1]["content"])
        properties = body["response_format"]["json_schema"]["schema"]["properties"]
        answer = decide(state, properties)
        payload = {
            "model": "mock-decider",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": json.dumps(answer)}}],
            "usage": {"prompt_tokens": 0, "completion_tokens": 0},
        }
        data = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *_args):
        pass


def main():
    global RISK
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8799)
    parser.add_argument("--risk", type=float, default=0.0, help="risk to report, to exercise the guard")
    options = parser.parse_args()
    RISK, port = options.risk, options.port
    server = HTTPServer(("127.0.0.1", port), Handler)
    print(f"mock decider on http://127.0.0.1:{port}/v1", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
