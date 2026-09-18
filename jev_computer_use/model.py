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

# Offered only when the window will not say what is in it, and strictly worse
# than everything above: the model invents a coordinate instead of selecting an
# index, so nothing can check the target before the click lands, and the text
# goes wherever the app's own focus happens to be. Every other operation is
# addressed to an element that was observed; these two are aimed at a picture.
PIXEL_CONTROLS = {
    "CLICK_POINT": (
        "Click a point in the screenshot, for something the window does not expose as an element. "
        "Give click_x and click_y in the screenshot's own pixel coordinates.",
        {"op": "CLICK_POINT"},
    ),
    "TYPE_KEYS": (
        "Type text as keystrokes into whatever the app has focused. There is no field to aim at, "
        "so only use this straight after clicking into one.",
        {"op": "TYPE_KEYS"},
    ),
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


def action_space(page, pixels=False):
    """One index per element; each operation carries only the targets it can use.

    Returns the table shown to the model, the per-operation target maps, and the
    targetless controls. `pixels` adds the screenshot operations, which are
    offered only when the tree has too little in it to work from.
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
        # The identity that must still hold at execution time. The identifier
        # is the strongest of these and the label the weakest, so all three go.
        expect = source["label"] or source["role"]
        identity = {"expect": expect, "expect_role": source["role"], "expect_id": source.get("identifier", "")}
        for operation in operations:
            if operation == "SELECT":
                for position, option in enumerate(source.get("options", [])):
                    targets.setdefault("SELECT", {})[option["index"]] = {
                        "operation": "SELECT",
                        "op": "SELECT",
                        "path": source["path"],
                        "option": option["child"],
                        **identity,
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
                    **identity,
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

    offered = dict(CONTROLS)
    if pixels:
        offered.update(PIXEL_CONTROLS)
    controls = {name: {"operation": name, **body} for name, (_, body) in offered.items()}
    return elements, targets, controls


def questions_for(targets, controls):
    descriptions = {**CONTROLS, **PIXEL_CONTROLS}
    operations = {name: TARGETED[name] for name in targets}
    operations.update({name: descriptions[name][0] for name in controls})
    operations["DONE"] = "Every requirement is visibly satisfied in the current window."
    operations["BLOCKED"] = "No offered operation can make progress."
    return operations


def head_properties(operations, targets):
    """The fields the answer must contain: the operation, and one head per operation."""
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
    if "CLICK_POINT" in operations:
        properties["click_x"] = {"type": ["number", "null"], "description": "Screenshot x, used only for CLICK_POINT."}
        properties["click_y"] = {"type": ["number", "null"], "description": "Screenshot y, used only for CLICK_POINT."}
        properties["keys_value"] = {
            "type": ["string", "null"],
            "description": TEXT_VALUE + " Used only if the operation is TYPE_KEYS.",
        }
    return properties


def schema_format(properties):
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


def object_format_instructions(properties):
    """Spell the schema out for a provider that guarantees JSON but not its shape.

    Nothing downstream trusts this: an answer outside the offered choices is
    refused by `choose`, exactly as an invalid schema answer would be.
    """
    lines = ["Reply with one JSON object and nothing else. Its keys, all required:"]
    for name in sorted(properties):
        spec = properties[name]
        if "enum" in spec:
            lines.append(f'- "{name}": exactly one of {json.dumps(spec["enum"], ensure_ascii=False)}')
        elif spec.get("type") == "number":
            lines.append(f'- "{name}": a number between 0 and 1')
        elif name == "type_text_value":
            lines.append(f'- "{name}": a string, or null when the operation is not TYPE_TEXT')
        else:
            lines.append(f'- "{name}": a short string')
        if spec.get("description"):
            lines.append(f"    {spec['description']}")
    return "\n".join(lines)


def reasoning_body(base_url):
    """Turn extended thinking off where it is on by default.

    Picking an operation from an enumerated table is not a reasoning task, and
    a model that thinks first spends its whole output budget doing it: a
    `deepseek-flash` answer measured here was 417 reasoning tokens to 13 tokens
    of JSON, and on a real action space the JSON is what gets truncated.

    `DECISION_REASONING=default` leaves the provider's own behaviour alone.
    """
    if os.environ.get("DECISION_REASONING") == "default":
        return {}
    if "deepseek" in base_url:
        return {"thinking": {"type": "disabled"}}
    return {}


def uses_json_schema(base_url):
    """Whether this endpoint can constrain the answer server-side.

    `DECISION_RESPONSE_FORMAT` overrides the guess. DeepSeek, for one,
    guarantees valid JSON but not a given schema.
    """
    override = os.environ.get("DECISION_RESPONSE_FORMAT")
    if override:
        return override == "json_schema"
    return "deepseek" not in base_url


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
            total = sum(weights.values())
            # Every alternative can underflow to zero, which is not a distribution.
            if chosen in weights and len(weights) > 1 and total > 0:
                return {name: round(value / total, 4) for name, value in sorted(weights.items())}
        return {}
    return {}


def user_content(state, capture):
    """The observed state, with the picture attached when there is one."""
    text = json.dumps(state, ensure_ascii=False)
    if not capture:
        return text
    return [
        {"type": "text", "text": text},
        {
            "type": "image_url",
            "image_url": {"url": f"data:{capture['media_type']};base64,{capture['image']}"},
        },
    ]


def choose(page, goal, history, capture=None):
    """One request: the operation, a target for every operation, and a risk rating.

    `capture` is a screenshot of the window, passed only when the tree is too
    sparse to work from. It adds the pixel operations and costs an image.
    """
    elements, targets, controls = action_space(page, pixels=bool(capture))
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
    if capture:
        state["screenshot"] = {
            "why": "This window exposes almost nothing to the accessibility tree, so most of what "
            "you can see in the picture has no element index. Prefer an indexed element when one "
            "fits; fall back to CLICK_POINT only for what the table does not contain.",
            "width": capture["image_width"],
            "height": capture["image_height"],
            "coordinates": "Top-left origin. click_x is 0 to width, click_y is 0 to height.",
        }
    base = os.environ.get("DECISION_BASE_URL", "https://api.openai.com/v1").rstrip("/")
    key = os.environ.get("DECISION_API_KEY")
    properties = head_properties(operations, targets)
    strict = uses_json_schema(base)
    instructions = NEXT_ACTION if strict else NEXT_ACTION + "\n\n" + object_format_instructions(properties)
    body = {
        "model": os.environ.get("DECISION_MODEL", "gpt-5.6"),
        "temperature": 0,
        "max_tokens": 700,
        "logprobs": True,
        "top_logprobs": 8,
        "response_format": schema_format(properties) if strict else {"type": "json_object"},
        **reasoning_body(base),
        "messages": [
            {"role": "system", "content": instructions},
            {"role": "user", "content": user_content(state, capture)},
        ],
    }
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
        action = dict(controls[operation])
        if operation == "CLICK_POINT":
            x, y = answer.get("click_x"), answer.get("click_y")
            if not all(isinstance(v, (int, float)) and math.isfinite(v) for v in (x, y)):
                raise ValueError("CLICK_POINT came back without a usable point; no operation executed.")
            width, height = capture["image_width"], capture["image_height"]
            if not (0 <= x <= width and 0 <= y <= height):
                raise ValueError(f"CLICK_POINT {x},{y} is outside the {width}x{height} capture; nothing executed.")
            # The scale travels with the capture the point was named in, so a
            # stale screenshot cannot be turned into a click somewhere else.
            action.update(x=float(x), y=float(y), scale=capture["scale"], label=f"point {int(x)},{int(y)}")
        elif operation == "TYPE_KEYS":
            action["label"] = "focused field"

    risk = answer.get("risk")
    if not isinstance(risk, (int, float)) or not math.isfinite(risk):
        risk = 1.0  # An unreadable rating is treated as the consequential case.
    return {
        "operation": operation,
        "target": target,
        "action": action,
        "text": answer.get("keys_value") if operation == "TYPE_KEYS" else answer.get("type_text_value"),
        "pixels": bool(capture) and operation in PIXEL_CONTROLS,
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
            **reasoning_body(base),
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
