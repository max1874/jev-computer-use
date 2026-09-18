"""The action space, and the single request that chooses an operation and a target.

One observation produces one indexed element table. One request asks, on the
same state, which operation to run and which target each operation would use.
The executor consumes only the head matching the chosen operation; the rest is
speculation that cost no extra round trip.
"""

import json
import math
import os
import time

import httpx

from .questions import GUARD, NEXT_ACTION, TARGET, TEXT_VALUE

CLIENT = httpx.Client(http2=True, timeout=60)

# Operations that need no target. They are offered alongside the target heads.
CONTROLS = {
    "SCROLL_DOWN": ("Scroll the main content down.", {"op": "SCROLL_DOWN"}),
    "SCROLL_UP": ("Scroll the main content up.", {"op": "SCROLL_UP"}),
    "PRESS_RETURN": ("Press Return, to commit the focused field or accept a dialog.", {"op": "KEY", "key": "return"}),
    "PRESS_ESCAPE": ("Press Escape, to dismiss an open menu, popover, or sheet.", {"op": "KEY", "key": "esc"}),
    "WAIT": ("Wait briefly for the interface to settle.", {"op": "WAIT"}),
}

TARGETED = {
    "PRESS": "Press an element: a button, checkbox, row, tab, link, or disclosure triangle.",
    "TYPE_TEXT": "Enter or replace the whole contents of an editable field.",
    "SELECT": "Choose a value from a pop-up button's menu.",
    "MENU": "Run a menu-bar command by name, without opening the menu first.",
}


def post_json(url, key, body):
    for attempt in range(3):
        try:
            response = CLIENT.post(url, json=body, headers={"Authorization": f"Bearer {key}"})
        except httpx.HTTPError:
            raise RuntimeError("Model connection failed; no operation executed.") from None
        if response.status_code in {408, 429, 500, 502, 503, 529} and attempt < 2:
            time.sleep(0.4 * 2**attempt)
            continue
        if response.is_error:
            detail = response.text[:300]
            raise RuntimeError(f"Model provider returned HTTP {response.status_code}: {detail}")
        return response.json()
    raise RuntimeError("Model unavailable")


def action_space(page):
    """One index per element; each operation carries only the targets it can use.

    Returns the table shown to the model, the per-operation target maps, and the
    targetless controls.
    """
    elements, targets = [], {}
    for source in page["elements"]:
        operations = [op for op in source["operations"] if op in TARGETED]
        # An element with no usable operation is context, not a choice.
        if not operations and source["role"] != "AXTextArea":
            continue
        index = source["index"]
        shown = {
            "index": index,
            "role": source["role"].removeprefix("AX"),
            "label": source["label"] or source["role"].removeprefix("AX"),
            "operations": operations,
        }
        for key in ("value", "checked", "selected"):
            if source.get(key) not in (None, ""):
                shown[key] = source[key]
        # The guard that must still hold at execution time.
        expect = source["label"] or source["role"]
        for operation in operations:
            if operation == "SELECT":
                for position, option in enumerate(source.get("options", [])):
                    targets.setdefault("SELECT", {})[option["index"]] = {
                        "operation": "SELECT",
                        "op": "SELECT",
                        "path": source["path"],
                        "option": option["child"],
                        "expect": expect,
                        "label": f"{shown['label']} → {option['label']}",
                    }
                if source.get("options"):
                    shown["options"] = [o["label"] for o in source["options"]]
                else:
                    shown["operations"] = [o for o in operations if o != "SELECT"]
            else:
                targets.setdefault(operation, {})[index] = {
                    "operation": operation,
                    "op": operation,
                    "path": source["path"],
                    "expect": expect,
                    "label": shown["label"],
                    "role": shown["role"],
                    "value": shown.get("value", ""),
                }
        elements.append(shown)

    for item in page.get("menus", []):
        targets.setdefault("MENU", {})[item["index"]] = {
            "operation": "MENU",
            "op": "MENU",
            "menu": item["path"],
            "label": item["label"],
        }

    controls = {name: {"operation": name, **body} for name, (_, body) in CONTROLS.items()}
    return elements, targets, controls


def questions_for(targets, controls):
    operations = {name: TARGETED[name] for name in targets}
    operations.update({name: CONTROLS[name][0] for name in controls})
    operations["DONE"] = "Every requirement is visibly satisfied in the current window."
    operations["BLOCKED"] = "No offered operation can make progress."
    return operations


def response_schema(operations, targets):
    properties = {
        "operation": {"type": "string", "enum": sorted(operations)},
        "confidence": {"type": "number", "description": "0-1, how sure the operation is right"},
        "risk": {"type": "number", "description": GUARD},
        "risk_reason": {"type": "string", "description": "One clause naming what this operation changes."},
    }
    for operation, candidates in targets.items():
        properties[operation.lower() + "_target"] = {
            "type": "string",
            "enum": sorted(candidates),
            "description": f"{TARGET}\n\nThe operation assumed by this answer is {operation}.",
        }
    if "TYPE_TEXT" in targets:
        properties["type_text_value"] = {
            "type": ["string", "null"],
            "description": TEXT_VALUE + " Used only if the operation is TYPE_TEXT.",
        }
    return {
        "type": "json_schema",
        "json_schema": {
            "name": "next_operation",
            "strict": True,
            "schema": {
                "type": "object",
                "properties": properties,
                "required": sorted(properties),
                "additionalProperties": False,
            },
        },
    }


def operation_distribution(payload, chosen, operations):
    """Best-effort probabilities over the operation head, from token logprobs.

    Providers that do not return logprobs simply get no distribution; the
    inspector then shows the model's own confidence instead.
    """
    try:
        tokens = payload["choices"][0]["logprobs"]["content"]
    except (KeyError, IndexError, TypeError):
        return {}
    seen = ""
    for position, token in enumerate(tokens):
        seen += token.get("token", "")
        if not seen.rstrip().endswith('"operation":') and not seen.rstrip().endswith('"operation": "'):
            continue
        # The value's first token discriminates between the candidates.
        for candidate in tokens[position:][:3]:
            alternatives = candidate.get("top_logprobs") or []
            if not alternatives:
                continue
            weights = {}
            for alternative in alternatives:
                piece = alternative["token"].strip().strip('"')
                if not piece:
                    continue
                matches = [name for name in operations if name.startswith(piece)]
                if not matches:
                    continue
                share = math.exp(alternative["logprob"]) / len(matches)
                for name in matches:
                    weights[name] = weights.get(name, 0.0) + share
            if chosen in weights and len(weights) > 1:
                total = sum(weights.values())
                return {name: round(value / total, 4) for name, value in sorted(weights.items())}
        return {}
    return {}


def choose(page, goal, history):
    """One request: the operation, a target for every operation, and a risk rating."""
    elements, targets, controls = action_space(page)
    operations = questions_for(targets, controls)
    state = {
        "goal": goal,
        "window": {"app": page["app"], "title": page["window"], "visible_text": page["text"]},
        "elements": elements,
        "menu_commands": [{"index": i["index"], "command": i["label"]} for i in page.get("menus", [])],
        "recent_actions": [
            {k: h.get(k) for k in ("operation", "label", "text", "window_changed")} for h in history[-10:]
        ],
        "offered_operations": operations,
    }
    body = {
        "model": os.environ.get("DECISION_MODEL", "gpt-5.6"),
        "temperature": 0,
        "max_tokens": 700,
        "logprobs": True,
        "top_logprobs": 8,
        "response_format": response_schema(operations, targets),
        "messages": [
            {"role": "system", "content": NEXT_ACTION},
            {"role": "user", "content": json.dumps(state, ensure_ascii=False)},
        ],
    }
    base = os.environ.get("DECISION_BASE_URL", "https://api.openai.com/v1").rstrip("/")
    key = os.environ.get("DECISION_API_KEY")
    if not key:
        raise RuntimeError("Set DECISION_API_KEY (and DECISION_BASE_URL / DECISION_MODEL) before running.")
    started = time.perf_counter()
    payload = post_json(base + "/chat/completions", key, body)
    latency = round((time.perf_counter() - started) * 1000)
    try:
        answer = json.loads(payload["choices"][0]["message"]["content"])
    except (KeyError, IndexError, ValueError):
        raise ValueError("The decision model returned no parseable answer; no operation executed.") from None

    operation = answer.get("operation")
    if operation not in operations:
        raise ValueError(f"The decision model chose {operation!r}, which was not offered; no operation executed.")

    action, target = None, None
    if operation in targets:
        target = answer.get(operation.lower() + "_target")
        if target not in targets[operation]:
            raise ValueError(f"{operation} target {target!r} was not offered; no operation executed.")
        action = targets[operation][target]
    elif operation in controls:
        action = controls[operation]

    risk = answer.get("risk")
    if not isinstance(risk, (int, float)) or not math.isfinite(risk):
        risk = 1.0  # An unreadable rating is treated as the consequential case.
    return {
        "operation": operation,
        "target": target,
        "action": action,
        "text": answer.get("type_text_value"),
        "confidence": max(0.0, min(1.0, float(answer.get("confidence") or 0))),
        "risk": max(0.0, min(1.0, float(risk))),
        "risk_reason": str(answer.get("risk_reason") or "")[:300],
        "probabilities": operation_distribution(payload, operation, operations),
        "model": payload.get("model", body["model"]),
        "usage": payload.get("usage", {}),
        "latency_ms": latency,
        "offered": {"operations": sorted(operations), "targets": {k: sorted(v) for k, v in targets.items()}},
    }


def field_context(goal, action, page, history):
    return {
        "goal": goal,
        "field": {k: action.get(k) for k in ("label", "role", "value")},
        "window": {"title": page["window"], "text": page["text"][:4000]},
        "recent_actions": [{k: h.get(k) for k in ("label", "text")} for h in history[-6:]],
    }


def field_text(context):
    """Fallback for a decision backend that only chooses and cannot write text."""
    key = os.environ.get("TEXT_MODEL_API_KEY") or os.environ.get("DECISION_API_KEY")
    if not key:
        raise ValueError("TYPE_TEXT needs a text model; no value is hardcoded or guessed by the executor.")
    base = os.environ.get("TEXT_MODEL_BASE_URL", os.environ.get("DECISION_BASE_URL", "https://api.openai.com/v1"))
    model = os.environ.get("TEXT_MODEL", os.environ.get("DECISION_MODEL", "gpt-5.6"))
    started = time.perf_counter()
    payload = post_json(
        base.rstrip("/") + "/chat/completions",
        key,
        {
            "model": model,
            "max_tokens": 512,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": TEXT_VALUE + ' Reply as {"text": "..."} or {"text": null}.'},
                {"role": "user", "content": json.dumps(context, ensure_ascii=False)},
            ],
        },
    )
    try:
        output = json.loads(payload["choices"][0]["message"]["content"])
        value = output["text"]
        if set(output) != {"text"} or not isinstance(value, str) or not value.strip() or len(value) > 2000:
            raise ValueError()
    except (ValueError, KeyError, TypeError, IndexError):
        raise ValueError("The text model returned no valid field value; nothing was typed.") from None
    return value, {"model": model, "latency_ms": round((time.perf_counter() - started) * 1000)}
