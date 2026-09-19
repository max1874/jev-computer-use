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

from .questions import GUARD, GUARD_CHOICE, GUARD_LEVELS, NEXT_ACTION, TARGET, TEXT_VALUE

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
    # INCREMENT and DECREMENT are deliberately absent. The bridge derives and
    # executes both, and adding them here is one line — it was added, measured,
    # and taken out again. Calculator publishes them on "Show Sidebar" and
    # "Mode", so offering them put two more heads in every answer and two more
    # ways to be wrong in a task that needs neither. On "compute 12 times 34",
    # against deepseek-flash: 6 of 6 runs correct in 6 operations without them,
    # 1 of 8 with them, the rest wandering into the 40-operation budget. The
    # element table was identical in both. See "What is not offered".
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


def scaffolding(candidates):
    """Paths of the unnamed elements that exist only to contain another one.

    A Chromium interface is built out of nested anonymous containers, and the
    accessibility tree reports each one as a pressable group with no name. Laid
    out as a flat indexed table they become a hundred choices that read alike
    and differ only by number — Lark offered 143 elements of which 121 were
    these. They are not choices. Whatever they wrap is the choice, and it is in
    the table too.

    A container is dropped when something else in the table sits inside it and
    the container either has no name of its own, or has the same name as the
    thing inside it. The second case is the common one once unnamed containers
    take their name from the words underneath: a row and the single box inside
    it both come back reading "HJDM", and only the inner one should be offered.

    A group with no name and no words under it is dropped outright. A group is
    a container by definition, and one that cannot say what it contains is not
    something a model can choose: in Lark these are the 24x21 avatars inside
    chat rows that are already offered by name, and the sidebar itself. An
    unnamed *button* is kept — that is an icon-only control, and nothing else
    in the table reaches it.
    """
    drop = {
        candidate["path"]
        for candidate in candidates
        if candidate["role"] == "AXGroup" and not candidate["label"] and not candidate.get("value")
    }
    for candidate in candidates:
        inside = [
            other for other in candidates if other["path"].startswith(candidate["path"] + ".")
        ]
        if not inside:
            continue
        if not candidate["label"] or any(other["label"] == candidate["label"] for other in inside):
            drop.add(candidate["path"])
    return drop


def action_space(page, pixels=False):
    """One index per element; each operation carries only the targets it can use.

    Returns the table shown to the model, the per-operation target maps, and the
    targetless controls. `pixels` adds the screenshot operations, which are
    offered only when the tree has too little in it to work from.
    """
    usable = [
        source
        for source in page["elements"]
        # An element with no usable operation is context, not a choice.
        if [op for op in source["operations"] if op in TARGETED] or source["role"] == "AXTextArea"
    ]
    wrappers = scaffolding(usable)

    elements, targets = [], {}
    for source in usable:
        if source["path"] in wrappers:
            continue
        operations = [op for op in source["operations"] if op in TARGETED]
        index = source["index"]
        shown = {"index": index, "role": source["role"].removeprefix("AX")}
        # No invented label. Writing the role into the label field turned a
        # hundred anonymous containers into a hundred elements all called
        # "Group", which reads as a table of real choices and is not one.
        if source["label"]:
            shown["label"] = source["label"]
        shown["operations"] = operations
        name = source["label"] or source["role"].removeprefix("AX")
        for key in ("value", "checked", "selected"):
            value = source.get(key)
            # `checked` is about the control's own state, so False is news.
            # `selected` is False on almost everything and says nothing.
            if value in (None, "") or (key == "selected" and not value):
                continue
            shown[key] = value
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
                        "label": f"{name} → {option['label']}",
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
                    "label": name,
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
    # These four are speculative in the same way the target heads are: answer
    # them as if their operation is the one that runs, whichever one you then
    # choose. Saying "used only for CLICK_POINT" instead invited the answer
    # null, and the shape makes that worse than it sounds — the keys are
    # written in a fixed order with `operation` first now, but they used to be
    # alphabetical, so a model emitted click_x before it had committed to an
    # operation, wrote null because it had not chosen CLICK_POINT yet, and then
    # chose CLICK_POINT. Every pixel decision came back with no point in it.
    if "TYPE_TEXT" in targets:
        properties["type_text_value"] = {
            "type": ["string", "null"],
            "description": TEXT_VALUE + " Answer as if TYPE_TEXT is the operation that runs.",
        }
    if "CLICK_POINT" in operations:
        point = "Answer as if CLICK_POINT is the operation that runs, whichever operation you choose."
        properties["click_x"] = {
            "type": ["number", "null"],
            "description": f"Screenshot x of the point to click. {point}",
        }
        properties["click_y"] = {
            "type": ["number", "null"],
            "description": f"Screenshot y of the point to click. {point}",
        }
        properties["keys_value"] = {
            "type": ["string", "null"],
            "description": TEXT_VALUE + " Answer as if TYPE_KEYS is the operation that runs.",
        }
    return properties


def head_order(properties):
    """`operation` first, then the rest by name.

    A model writes the object in the order it is given, and every other head is
    conditioned on the operation. Asking for them alphabetically asked each one
    to be answered before the thing it depends on had been decided.
    """
    return ["operation"] + sorted(name for name in properties if name != "operation")


def schema_format(properties):
    return {
        "type": "json_schema",
        "json_schema": {
            "name": "next_operation",
            "strict": True,
            "schema": {
                "type": "object",
                "properties": {name: properties[name] for name in head_order(properties)},
                "required": head_order(properties),
                "additionalProperties": False,
            },
        },
    }


def object_format_instructions(properties):
    """Spell the schema out for a provider that guarantees JSON but not its shape.

    Nothing downstream trusts this: an answer outside the offered choices is
    refused by `choose`, exactly as an invalid schema answer would be.
    """
    lines = [
        "Reply with one JSON object and nothing else. Write the keys in the order given,",
        "all of them required. Every key after the first is answered as if its own operation",
        "is the one that runs, whichever one you choose in the first:",
    ]
    for name in head_order(properties):
        spec = properties[name]
        kinds = spec.get("type")
        kinds = kinds if isinstance(kinds, list) else [kinds]
        if "enum" in spec:
            lines.append(f'- "{name}": exactly one of {json.dumps(spec["enum"], ensure_ascii=False)}')
        elif "number" in kinds:
            # A head typed number-or-null used to fall past this test and be
            # described as a short string, which is what `click_x` and
            # `click_y` are. The model was told to send text for a coordinate
            # and `choose` then refused the text for not being a number, so on
            # every provider that spells the shape out rather than enforcing it
            # — which is the one the README recommends — the pixel path could
            # choose CLICK_POINT and never once execute it.
            # No mention of null here, even though the type permits it. This
            # head is speculative, and the answer null is what "only used for"
            # wording produced: never a point, on every request.
            lines.append(f'- "{name}": a number')
        elif name == "type_text_value":
            lines.append(f'- "{name}": a string, or null when the operation is not TYPE_TEXT')
        else:
            lines.append(f'- "{name}": a short string')
        if spec.get("description"):
            lines.append(f"    {spec['description']}")
    return "\n".join(lines)


def names(needle, base_url, model):
    """Whether the provider or the model is the one named.

    Both of the checks below used to read the base URL alone, which is right
    until the same model arrives through a gateway. `deepseek-v4.1-flash`
    served from openrouter.ai is still DeepSeek and the URL no longer says so,
    so both checks silently took the wrong branch and every run through that
    gateway failed the same way: reasoning left on, the output budget spent on
    it, the JSON truncated, "no parseable answer". That is the exact failure
    the note in `reasoning_body` describes, arriving by the one route its own
    check could not see.
    """
    return needle in (base_url or "").lower() or needle in (model or "").lower()


def reasoning_body(base_url, model):
    """Turn extended thinking off where it is on by default.

    Picking an operation from an enumerated table is not a reasoning task, and
    a model that thinks first spends its whole output budget doing it: a
    `deepseek-flash` answer measured here was 417 reasoning tokens to 13 tokens
    of JSON, and on a real action space the JSON is what gets truncated.

    `DECISION_REASONING=default` leaves the provider's own behaviour alone.
    """
    if os.environ.get("DECISION_REASONING") == "default":
        return {}
    if names("deepseek", base_url, model):
        # Two spellings, because the gateway and the vendor disagree and
        # sending the one the endpoint does not know is ignored, not refused.
        return {"thinking": {"type": "disabled"}, "reasoning": {"enabled": False}}
    return {}


def uses_json_schema(base_url, model=""):
    """Whether this endpoint can constrain the answer server-side.

    `DECISION_RESPONSE_FORMAT` overrides the guess. DeepSeek, for one,
    guarantees valid JSON but not a given schema.
    """
    override = os.environ.get("DECISION_RESPONSE_FORMAT")
    if override:
        return override == "json_schema"
    return not names("deepseek", base_url, model)


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


def number(value):
    """A finite number, whether it arrived as one or as a string of one."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, str):
        try:
            value = float(value.strip())
        except ValueError:
            return None
    if not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    return float(value)


def jev_questions(operations, targets):
    """The same heads, as named questions instead of fields of one JSON object.

    The shape barely has to be translated, which is the point: this project's
    decision was always an operation chosen from a set and one index chosen per
    operation. Writing that as a schema and asking a chat model to fill it in
    is a way of getting a choice out of something built to write prose. Here
    the choice is what the endpoint returns.
    """
    questions = {
        "operation": {"type": "choice", "instructions": NEXT_ACTION, "criteria": dict(operations)},
        "risk": {"type": "score", "instructions": GUARD_CHOICE, "criteria": list(GUARD_LEVELS)},
    }
    for operation, candidates in targets.items():
        questions[operation.lower() + "_target"] = {
            "type": "choice",
            "instructions": f"{TARGET}\n\nThe operation assumed by this answer is {operation}.",
            # Criteria keys are JSON object keys and so are strings; the action
            # space keys them by index, which is what they are turned back into.
            "criteria": {str(key): candidate.get("label") for key, candidate in candidates.items()},
        }
    return questions


def jev_reason(rating):
    """The level a score mostly landed on, in the level's own words.

    A backend that cannot write a sentence can still say which of the three
    tiers it read the operation as, and that is all `risk_reason` ever was.
    """
    probabilities = rating.get("probabilities") or {}
    if not probabilities:
        return ""
    top = max(probabilities, key=lambda level: probabilities[level])
    return (rating.get("legend") or {}).get(top, "")


def ask_jev(state, operations, targets):
    """The System One backend: every head answered with a choice and a distribution.

    Nothing here asks for JSON, because nothing here asks for writing. The
    probabilities come back measured rather than reconstructed from token
    logprobs, and the risk rating lands between its levels on its own instead
    of a model picking a round number.

    It takes text only, so the pixel path is not routed here — see `choose`.
    """
    base = os.environ.get("JEV_BASE_URL", "https://api.typesafe.ai/v1").rstrip("/")
    key = os.environ.get("JEV_API_KEY")
    body = {
        "state": state,
        "model": os.environ.get("JEV_MODEL", "jev-latest"),
        "questions": jev_questions(operations, targets),
    }
    started = time.perf_counter()
    payload = post_json(base + "/systemone", key, body)
    latency = round((time.perf_counter() - started) * 1000)
    answers = payload.get("answers") or {}
    if not isinstance(answers, dict) or "operation" not in answers:
        raise ValueError("The decision model returned no operation; no operation executed.")

    picked = answers["operation"]
    answer = {"operation": picked.get("choice"), "confidence": picked.get("confidence")}
    for name, reply in answers.items():
        if not name.endswith("_target"):
            continue
        candidates = targets.get(name[: -len("_target")].upper(), {})
        by_text = {str(index): index for index in candidates}
        answer[name] = by_text.get(reply.get("choice"), reply.get("choice"))

    rating = answers.get("risk") or {}
    if isinstance(rating.get("score"), (int, float)):
        answer["risk"] = rating["score"] / max(1, len(GUARD_LEVELS) - 1)
    answer["risk_reason"] = jev_reason(rating)
    return {
        "answer": answer,
        "probabilities": picked.get("probabilities") or {},
        "model": payload.get("model", body["model"]),
        "usage": payload.get("usage", {}),
        "latency_ms": latency,
    }


def ask_chat(state, operations, targets, capture):
    """The OpenAI-compatible backend: one JSON object carrying every head."""
    base = os.environ.get("DECISION_BASE_URL", "https://api.openai.com/v1").rstrip("/")
    key = os.environ.get("DECISION_API_KEY")
    if not key:
        raise RuntimeError("Set DECISION_API_KEY (and DECISION_BASE_URL / DECISION_MODEL) before running.")
    model = os.environ.get("DECISION_MODEL", "gpt-5.6")
    properties = head_properties(operations, targets)
    strict = uses_json_schema(base, model)
    instructions = NEXT_ACTION if strict else NEXT_ACTION + "\n\n" + object_format_instructions(properties)
    body = {
        "model": model,
        "temperature": 0,
        "max_tokens": 700,
        "logprobs": True,
        "top_logprobs": 8,
        "response_format": schema_format(properties) if strict else {"type": "json_object"},
        **reasoning_body(base, model),
        "messages": [
            {"role": "system", "content": instructions},
            {"role": "user", "content": user_content(state, capture)},
        ],
    }
    started = time.perf_counter()
    payload = post_json(base + "/chat/completions", key, body)
    latency = round((time.perf_counter() - started) * 1000)
    try:
        answer = json.loads(payload["choices"][0]["message"]["content"])
    except (KeyError, IndexError, ValueError):
        raise ValueError("The decision model returned no parseable answer; no operation executed.") from None
    return {
        "answer": answer,
        "probabilities": operation_distribution(payload, answer.get("operation"), operations),
        "model": payload.get("model", body["model"]),
        "usage": payload.get("usage", {}),
        "latency_ms": latency,
    }


def past_action(step):
    """One history row as the model sees it.

    `outcome` is carried only when the step has one, which is only when it did
    not run cleanly. Without it a refused operation and one that ran and moved
    nothing are the same row — `window_changed: false` — and the obvious read of
    that row is to try the same control again.
    """
    row = {k: step.get(k) for k in ("operation", "label", "text", "window_changed")}
    if step.get("outcome"):
        row["outcome"] = step["outcome"]
    return row


def choose(page, goal, history, capture=None, pointer=False):
    """One request: the operation, a target for every operation, and a risk rating.

    `capture` is a screenshot of the window, passed when the tree is too sparse
    to work from. `pointer` says whether the operations aimed at that picture
    may be offered, and the two are separate on purpose. Photographing a window
    needs nothing from the user — `screencapture` reads a window that is behind
    everything else — while clicking a point in it, as delivered here, needs the
    app in front. Tying them together meant a window could not be looked at
    without being raised, and the looking is the half that costs nothing.
    """
    elements, targets, controls = action_space(page, pixels=bool(capture) and pointer)
    operations = questions_for(targets, controls)
    state = {
        "goal": goal,
        "window": {"app": page["app"], "title": page["window"], "visible_text": page["text"]},
        "elements": elements,
        "menu_commands": [{"index": i["index"], "command": i["label"]} for i in page.get("menus", [])],
        # `outcome` only rides along when there is one, which is when the step
        # did not run cleanly. Without it a refusal and a press that landed on
        # something inert are the same row — `window_changed: false` — and the
        # obvious next move from that row is to press the same thing again.
        "recent_actions": [past_action(h) for h in history[-10:]],
        "offered_operations": operations,
    }
    # Only when it happened. The bridge has always reported this and nothing
    # read it, so a window with more elements than the limit allows reached the
    # model looking complete — the one kind of wrong table that cannot be
    # noticed from the inside. Said conditionally, so a window that fits pays
    # nothing for the warning.
    if page.get("truncated"):
        state["table_is_incomplete"] = (
            f"This window has more elements than the {len(elements)} listed; the rest were cut off. "
            "What you are looking for may exist and not be here. Scrolling brings different "
            "elements into view. Choosing something that merely looks close is worse than BLOCKED."
        )
    if capture:
        state["screenshot"] = {
            "why": (
                "This window exposes almost nothing to the accessibility tree, so most of what "
                "you can see in the picture has no element index. Prefer an indexed element when "
                "one fits; fall back to CLICK_POINT only for what the table does not contain."
                if pointer
                else "This window exposes almost nothing to the accessibility tree. The picture is "
                "here to be read, not aimed at: nothing in it can be clicked, because clicking a "
                "point needs the app in front and this run was not given permission to raise it. "
                "Use it to understand what the few offered elements are, and to say BLOCKED with a "
                "reason rather than guessing."
            ),
            "width": capture["image_width"],
            "height": capture["image_height"],
        }
        if pointer:
            state["screenshot"]["coordinates"] = (
                "Top-left origin. click_x is 0 to width, click_y is 0 to height."
            )
    # Which backend answers is decided by whether the window can be described
    # in text at all. A System One model returns a choice, which is the whole
    # shape of this decision and the argument the README makes; it also takes
    # text only. When the tree is too sparse to describe, the choice has to be
    # read off a picture, and that goes to the multimodal backend instead — the
    # one path where writing the answer out is the price of being able to see.
    if capture or not os.environ.get("JEV_API_KEY"):
        reply = ask_chat(state, operations, targets, capture)
    else:
        reply = ask_jev(state, operations, targets)
    answer = reply["answer"]

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
            # A number sent as "136" is a formatting habit, not a different
            # answer, and the bounds check below is what actually decides
            # whether the point is usable. Refusing the string only meant
            # refusing the provider.
            x, y = (number(answer.get(key)) for key in ("click_x", "click_y"))
            if x is None or y is None:
                raise ValueError("CLICK_POINT came back without a usable point; no operation executed.")
            width, height = capture["image_width"], capture["image_height"]
            if not (0 <= x <= width and 0 <= y <= height):
                raise ValueError(f"CLICK_POINT {x},{y} is outside the {width}x{height} capture; nothing executed.")
            # The scale travels with the capture the point was named in, so a
            # stale screenshot cannot be turned into a click somewhere else.
            # The window's identity and size travel with it too, because the
            # scale alone is not enough: a window that was resized between the
            # picture and the click maps the same point somewhere else, and the
            # click would land by coincidence. The bridge has always refused
            # that; until this it was never given what it needed to notice.
            action.update(
                x=float(x),
                y=float(y),
                scale=capture["scale"],
                window_id=capture["window_id"],
                window_size=capture["window_size"],
                label=f"point {int(x)},{int(y)}",
            )
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
        "probabilities": reply["probabilities"],
        "model": reply["model"],
        "usage": reply["usage"],
        "latency_ms": reply["latency_ms"],
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
            **reasoning_body(base, model),
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
