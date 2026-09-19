"""A local inspector: the element table, the decision, and what actually ran.

Loopback only. Every request carries a token minted at startup, Host and Origin
are checked, and agent commands are serialised, because they drive a real app.
"""

import argparse
import json
import os
import secrets
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from .agent import Agent
from .desktop import Bridge

STATIC = Path(__file__).resolve().parent / "static"
TYPES = {".html": "text/html", ".js": "text/javascript", ".css": "text/css"}

TOKEN = secrets.token_urlsafe(24)
LOCK = threading.Lock()
AGENT = {"agent": None}


def serialise(agent):
    if agent is None:
        return {"status": "idle", "history": [], "elements": [], "menus": [], "decisions": []}
    state = agent.snapshot()
    decision = state.get("decision")
    return {
        "status": state["status"],
        "app": state["app"],
        "goal": state["goal"],
        "window": state["window"],
        "elements": state["elements"],
        "menus": state["menus"][:60],
        "targets": state["targets"],
        "history": state["history"],
        "elapsed_ms": state["elapsed_ms"],
        "decision": None
        if not decision
        else {
            "operation": decision["operation"],
            "target": decision["target"],
            "text": decision["text"],
            "confidence": decision["confidence"],
            "risk": decision["risk"],
            "risk_reason": decision["risk_reason"],
            "probabilities": decision["probabilities"],
            "latency_ms": decision["latency_ms"],
            "model": decision["model"],
            "offered": decision["offered"]["operations"],
            "label": (decision["action"] or {}).get("label"),
        },
    }


class Handler(BaseHTTPRequestHandler):
    server_version = "open-computer-use"

    def guard(self):
        host = (self.headers.get("Host") or "").split(":")[0]
        if host not in {"127.0.0.1", "localhost"}:
            self.fail(403, "loopback only")
            return False
        origin = self.headers.get("Origin")
        if origin and not origin.startswith(("http://127.0.0.1", "http://localhost")):
            self.fail(403, "cross-origin request refused")
            return False
        return True

    def fail(self, code, message):
        self.reply(code, {"error": message})

    def reply(self, code, payload):
        data = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if not self.guard():
            return
        path = self.path.split("?")[0]
        if path == "/api/state":
            with LOCK:
                return self.reply(200, serialise(AGENT["agent"]))
        if path == "/api/apps":
            bridge = Bridge()
            try:
                apps = bridge.call("apps")["apps"]
            finally:
                bridge.close()
            return self.reply(200, {"apps": sorted(apps, key=lambda a: a["name"].lower())})
        name = "index.html" if path == "/" else path.lstrip("/")
        target = (STATIC / name).resolve()
        if not str(target).startswith(str(STATIC)) or not target.is_file():
            return self.fail(404, "not found")
        body = target.read_bytes()
        if target.name == "index.html":
            body = body.replace(b"__TOKEN__", TOKEN.encode())
        self.send_response(200)
        self.send_header("Content-Type", TYPES.get(target.suffix, "application/octet-stream"))
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        if not self.guard():
            return
        if self.headers.get("X-Token") != TOKEN:
            return self.fail(403, "bad token")
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length) or "{}")
        path = self.path.split("?")[0]
        # One command at a time: these drive a real application.
        with LOCK:
            try:
                if path == "/api/start":
                    if AGENT["agent"]:
                        AGENT["agent"].close()
                    AGENT["agent"] = Agent(
                        body["app"],
                        body["goal"],
                        activate=bool(body.get("activate")),
                        risk_threshold=float(body.get("risk_threshold", 0.5)),
                    )
                elif path == "/api/stop":
                    if AGENT["agent"]:
                        AGENT["agent"].close()
                    AGENT["agent"] = None
                elif path in {"/api/tick", "/api/predict", "/api/act"}:
                    agent = AGENT["agent"]
                    if not agent:
                        return self.fail(400, "start a run first")
                    if path == "/api/act":
                        agent.command("act", {"fingerprint": agent.state["page"]["fingerprint"]})
                    else:
                        agent.command(path.rsplit("/", 1)[1])
                elif path == "/api/approve":
                    agent = AGENT["agent"]
                    if not agent:
                        return self.fail(400, "start a run first")
                    agent.approve_pending()
                else:
                    return self.fail(404, "unknown command")
            except Exception as error:  # surfaced in the inspector, not swallowed
                payload = serialise(AGENT["agent"])
                payload["error"] = f"{type(error).__name__}: {error}"
                return self.reply(200, payload)
            return self.reply(200, serialise(AGENT["agent"]))

    def log_message(self, *_args):
        pass


def main():
    parser = argparse.ArgumentParser(description="Local inspector for open-computer-use")
    parser.add_argument("--port", type=int, default=int(os.environ.get("OPEN_COMPUTER_USE_PORT", os.environ.get("JEV_CU_PORT", 8767))))
    parser.add_argument("--no-open", action="store_true")
    options = parser.parse_args()
    server = HTTPServer(("127.0.0.1", options.port), Handler)
    url = f"http://127.0.0.1:{options.port}/"
    print(f"inspector on {url}")
    if not options.no_open:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        if AGENT["agent"]:
            AGENT["agent"].close()


if __name__ == "__main__":
    main()
