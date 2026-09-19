const token = document.body.dataset.token;
const $ = (id) => document.getElementById(id);
let running = false;
let latest = null;

async function api(path, body) {
  const options = body
    ? { method: "POST", headers: { "Content-Type": "application/json", "X-Token": token }, body: JSON.stringify(body) }
    : {};
  const response = await fetch(path, options);
  const payload = await response.json();
  if (payload.error) banner(payload.error);
  else banner(null);
  return payload;
}

function banner(message) {
  const element = $("banner");
  element.hidden = !message;
  element.textContent = message || "";
}

function escape(value) {
  return String(value ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
}

function renderElements(state) {
  const chosen = state.decision && state.decision.target;
  // A cut-off table is the one kind of wrong table that looks right, so say so
  // where the count is, rather than leaving the reader to wonder.
  const cut = state.window && state.window.truncated;
  $("count").textContent = state.elements.length
    ? `${state.elements.length} indexed${cut ? " · cut off at the limit" : ""}`
    : "";
  $("count").classList.toggle("warn", Boolean(cut));
  $("elements").innerHTML =
    state.elements
      .map((element) => {
        const value = element.checked !== undefined ? (element.checked ? "checked" : "unchecked") : element.value;
        const hit = chosen && (chosen === element.index || String(chosen).startsWith(element.index + ":"));
        return `<div class="row ${hit ? "chosen" : ""}">
          <span class="idx">[${escape(element.index)}]</span>
          <span class="name">${escape(element.label)}
            <span class="role">${escape(element.role)}</span>
            ${value ? `<span class="val">· ${escape(value)}</span>` : ""}</span>
          <span class="ops">${element.operations.map((op) => `<span class="op">${escape(op)}</span>`).join("")}</span>
        </div>`;
      })
      .join("") || `<div class="empty">No elements yet.</div>`;

  $("menu-count").textContent = state.menus && state.menus.length ? `${state.menus.length} shown` : "";
  $("menu-hint").textContent = state.menus && state.menus.length
    ? "Readable while the menus stay closed."
    : "None offered — the app is not active, so its menu bar reports everything disabled.";
  $("menus").innerHTML = (state.menus || [])
    .map(
      (item) =>
        `<div class="row ${chosen === item.index ? "chosen" : ""}">
          <span class="idx">[${escape(item.index)}]</span>
          <span class="name">${escape(item.label)}</span><span></span>
        </div>`
    )
    .join("");
}

function renderDecision(state) {
  const box = $("decision");
  const decision = state.decision;
  if (!decision) {
    box.className = "empty";
    box.textContent = state.status === "idle" ? "Nothing chosen yet." : "No decision pending.";
    return;
  }
  box.className = "decision";
  const probabilities = decision.probabilities || {};
  const entries = Object.entries(probabilities).sort((a, b) => b[1] - a[1]);
  const top = entries.length ? entries[0][0] : null;
  const bars = entries.length
    ? `<div class="bars">${entries
        .map(
          ([name, value]) => `<div class="bar ${name === top ? "top" : ""}">
            <span>${escape(name)}</span>
            <span class="track"><span class="fill" style="width:${(value * 100).toFixed(1)}%"></span></span>
            <span class="pct">${(value * 100).toFixed(0)}%</span>
          </div>`
        )
        .join("")}</div>`
    : `<div class="hint">This provider returned no logprobs, so there is no distribution to show.</div>`;

  const held = state.status === "needs_approval";
  box.innerHTML = `
    <div class="chosen-op">${escape(decision.operation)}
      <small>${escape(decision.label || "no target")}${decision.text ? ` — “${escape(decision.text)}”` : ""}</small>
    </div>
    ${bars}
    <div class="meta">
      <span>confidence ${(decision.confidence * 100).toFixed(0)}%</span>
      <span>${decision.latency_ms} ms</span>
      <span>${escape(decision.model)}</span>
      <span>${decision.offered.length} operations offered</span>
    </div>
    <div class="risk ${held ? "held" : ""}">
      <b>risk ${decision.risk.toFixed(2)}</b> — ${escape(decision.risk_reason || "not rated")}
      ${held ? `<div style="margin-top:8px"><button class="approve" id="approve">Approve and run it</button></div>` : ""}
    </div>`;
  if (held) $("approve").onclick = async () => render(await api("/api/approve", {}));
}

function renderHistory(state) {
  $("history").innerHTML =
    state.history
      .slice()
      .reverse()
      .map(
        (step) => `<div class="step ${step.window_changed === false ? "unchanged" : ""}">
          <span class="n">${step.step}</span>
          <span>${escape(step.operation)} · ${escape(step.label)}
            ${step.text ? `<span class="text">“${escape(step.text)}”</span>` : ""}
            ${step.window_changed === false ? `<span class="role">· nothing changed</span>` : ""}</span>
          <span class="ms">${step.elapsed_ms} ms</span>
        </div>`
      )
      .join("") || `<div class="empty">Nothing has run.</div>`;
}

function render(state) {
  latest = state;
  renderElements(state);
  renderDecision(state);
  renderHistory(state);
  $("status").textContent = state.status;
  $("status").className = "pill " + state.status;
  $("elapsed").textContent = state.elapsed_ms ? `${state.elapsed_ms} ms elapsed` : "";
  const live = !["idle", "done", "blocked"].includes(state.status);
  $("step").disabled = !live;
  $("run").disabled = !live || running;
  $("stop").disabled = state.status === "idle";
}

$("start").onclick = async () => {
  running = false;
  render(
    await api("/api/start", {
      app: $("app").value.trim() || "TextEdit",
      goal: $("goal").value.trim(),
      activate: $("activate").checked,
    })
  );
};

$("step").onclick = async () => render(await api("/api/tick", {}));

$("run").onclick = async () => {
  running = true;
  $("run").disabled = true;
  try {
    for (let i = 0; i < 40; i++) {
      const state = await api("/api/tick", {});
      render(state);
      if (["done", "blocked", "idle", "needs_approval"].includes(state.status)) break;
      if (state.error) break;
    }
  } finally {
    running = false;
    if (latest) render(latest);
  }
};

$("stop").onclick = async () => {
  running = false;
  render(await api("/api/stop", {}));
};

(async () => {
  const { apps } = await api("/api/apps");
  $("apps").innerHTML = apps.map((app) => `<option value="${escape(app.name)}"></option>`).join("");
  render(await api("/api/state"));
})();
