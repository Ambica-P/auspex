// Auspex ops console (live): polls the core's API and renders with render.js.
// Served by the core at /ui/. The judge site (web/) uses the same rendering (web/lib/render.js).
"use strict";

const { h, time } = Auspex;
const TOKEN_KEY = "auspex.token";
let token = null;
try { token = sessionStorage.getItem(TOKEN_KEY); } catch (_) { token = null; }
let selected = null;
let lastPlans = [];

async function api(path, opts = {}) {
  const res = await fetch(path, {
    ...opts,
    headers: { "Authorization": `Bearer ${token}`, "Content-Type": "application/json" },
  });
  if (res.status === 401) { logout("The token was refused. Enter it again."); throw new Error("401"); }
  if (!res.ok) throw new Error(`${res.status} ${await res.text()}`);
  return res.json();
}

// ---------------------------------------------------------------- login

function logout(message) {
  try { sessionStorage.removeItem(TOKEN_KEY); } catch (_) { /* private window */ }
  token = null;
  document.getElementById("app").hidden = true;
  document.getElementById("login").hidden = false;
  document.getElementById("login-error").textContent = message || "";
}

document.getElementById("login-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  token = document.getElementById("token").value.trim();
  try {
    await api("/api/v1/config");
    try { sessionStorage.setItem(TOKEN_KEY, token); } catch (_) { /* keep in memory */ }
    start();
  } catch (err) {
    if (String(err.message) !== "401") document.getElementById("login-error").textContent = `The core did not answer: ${err.message}`;
  }
});

// ---------------------------------------------------------------- decisions

function showPlans(plans) {
  lastPlans = plans;
  Auspex.renderPlans(document.getElementById("plans"), plans, selected, select);
}

async function select(planId) {
  selected = planId;
  showPlans(lastPlans);
  const detail = document.getElementById("detail");
  try {
    Auspex.renderDetail(detail, await api(`/api/v1/plans/${encodeURIComponent(planId)}/context`), { onDecide: decide });
  } catch (err) {
    detail.replaceChildren(h("p", { class: "error" }, `Could not load this decision: ${err.message}`));
  }
}

async function decide(planId, verb) {
  const body = verb === "approve" ? { approver: "ops-console" } : { approver: "ops-console", reason: "rejected in the console" };
  try {
    await api(`/api/v1/plans/${encodeURIComponent(planId)}/${verb}`, { method: "POST", body: JSON.stringify(body) });
  } catch (err) {
    alert(`Could not ${verb}: ${err.message}`);
  }
  await refreshPlans();
  await select(planId);
}

// ---------------------------------------------------------------- ledger, config

async function openLedger() {
  const dialog = document.getElementById("ledger-dialog");
  dialog.showModal();
  const status = document.getElementById("ledger-status");
  status.textContent = "Verifying the hash chain...";
  try {
    const v = await api("/api/v1/ledger/verify");
    status.textContent = v.ok ? `Chain verified: ${v.entries} entries, each hashed with the one before.` : `Chain broken: ${v.error}`;
    const entries = await api("/api/v1/ledger?limit=80");
    document.getElementById("ledger").replaceChildren(...entries.map((e) => h("li", {},
      h("span", {}, `#${e.seq}`), h("span", {}, `${time(e.recorded_at)} ${e.kind.replace("_", " ")}`),
      h("span", { class: "hash" }, e.entry_hash))));
  } catch (err) { status.textContent = `Could not read the ledger: ${err.message}`; }
}

async function setConfig(c) {
  await api("/api/v1/config", { method: "PUT", body: JSON.stringify({ config: c, by: "ops-console" }) });
  await refreshHealth();
}

// ---------------------------------------------------------------- polling

async function refreshHealth() {
  const hlt = await api("/api/v1/health");
  for (const b of document.querySelectorAll(".config button")) b.setAttribute("aria-pressed", String(b.dataset.config === hlt.config));
  document.getElementById("providers").textContent = `LLM ${hlt.llm_provider}, search ${hlt.search_provider}`;
}
async function refreshPicture() {
  const [tl, conflicts] = await Promise.all([api("/api/v1/timeline"), api("/api/v1/conflicts")]);
  Auspex.renderTimeline(document.getElementById("timeline"), tl, conflicts);
}
async function refreshPlans() { showPlans(await api("/api/v1/plans?limit=40")); }
async function refreshMap() {
  const note = Auspex.renderMap(document.getElementById("map"), await api("/api/v1/map"));
  if (note) document.getElementById("map-note").textContent = note;
}
async function refreshIncidents() { Auspex.renderIncidents(document.getElementById("incidents"), await api("/api/v1/incidents?limit=20")); }

function every(ms, fn) {
  const run = async () => { try { if (token) await fn(); } catch (err) { console.warn(err); } };
  run();
  return setInterval(run, ms);
}

function start() {
  document.getElementById("login").hidden = true;
  document.getElementById("app").hidden = false;
  for (const b of document.querySelectorAll(".config button")) b.addEventListener("click", () => setConfig(b.dataset.config));
  document.getElementById("verify").addEventListener("click", openLedger);
  document.getElementById("ledger-close").addEventListener("click", () => document.getElementById("ledger-dialog").close());
  setInterval(() => { document.getElementById("clock").textContent = new Date().toLocaleTimeString(); }, 1000);
  every(5000, refreshHealth);
  every(2000, refreshPicture);
  every(2000, refreshMap);
  every(3000, refreshPlans);
  every(5000, refreshIncidents);
  every(5000, async () => {
    const p = lastPlans.find((x) => x.plan_id === selected);
    if (p && ["awaiting_approval", "executing", "verifying"].includes(p.status)) await select(selected);
  });
}

if (token) start(); else logout("");
