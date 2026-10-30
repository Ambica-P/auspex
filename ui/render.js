// Auspex rendering shared by the live ops console (app.js) and the judge site (web/lib/replay-engine.js):
// timeline, decisions, decision detail, incidents, map. Every value from the API or a
// recording is rendered as text (never as HTML): job notes, destination text and vendor
// pages are untrusted.
"use strict";

const Auspex = (() => {
  const PAST_S = 120;
  const AHEAD_S = 720;

  function h(tag, attrs, ...children) {
    const el = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs || {})) {
      if (v === null || v === undefined || v === false) continue;
      if (k === "class") el.className = v;
      else if (k === "style") Object.assign(el.style, v);
      else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
      else el.setAttribute(k, v === true ? "" : String(v));
    }
    for (const c of children.flat(Infinity)) {
      if (c === null || c === undefined || c === false) continue;
      el.append(c instanceof Node ? c : document.createTextNode(String(c)));
    }
    return el;
  }

  function svg(tag, attrs, ...children) {
    const el = document.createElementNS("http://www.w3.org/2000/svg", tag);
    for (const [k, v] of Object.entries(attrs || {})) el.setAttribute(k, String(v));
    for (const c of children.flat(Infinity)) {
      if (c === null || c === undefined) continue;
      el.append(c instanceof Node ? c : document.createTextNode(String(c)));
    }
    return el;
  }

  const nodes = (...items) => items.flat(Infinity).filter((x) => x !== null && x !== undefined && x !== false);

  // Clock: the live console shows local time; the replay shows the scenario's clock.
  const clock = {
    format: (ms, seconds = true) => new Date(ms).toLocaleTimeString([], seconds
      ? { hour: "2-digit", minute: "2-digit", second: "2-digit" } : { hour: "2-digit", minute: "2-digit" }),
  };
  const time = (iso) => iso ? clock.format(new Date(iso).getTime()) : "–";
  const short = (urn) => String(urn || "").replace(/^job\//, "").replace(/^transport_system-/, "transport ").replace(/^delivery_requests-/, "delivery ");
  const seg = (urn) => String(urn || "").replace(/^segment\//, "");

  // ---------------------------------------------------------------- timeline

  function renderTimeline(root, tl, conflicts, nowMs) {
    const now = nowMs ?? (tl.at ? new Date(tl.at).getTime() : Date.now());
    const t0 = now - PAST_S * 1000;
    const span = (PAST_S + AHEAD_S) * 1000;
    const pct = (iso) => Math.max(0, Math.min(100, ((new Date(iso).getTime() - t0) / span) * 100));
    const segs = new Set(["segment/lift_25", "segment/lift_1"]);
    for (const c of conflicts) segs.add(c.segment_urn);
    for (const [s, claims] of Object.entries(tl.segments || {})) {
      if (claims.some((c) => String(c.actor_urn).startsWith("porter/"))) segs.add(s);
    }
    const ordered = [...segs].sort((a, b) => (a.includes("lobby") - b.includes("lobby")) || a.localeCompare(b));

    const ticks = h("div", { class: "tl-ticks" });
    for (let m = -2; m <= 10; m += 2) {
      ticks.append(h("span", { style: { left: `${((m * 60 + PAST_S) / (PAST_S + AHEAD_S)) * 100}%` } },
        m === 0 ? "now" : clock.format(now + m * 60000, false)));
    }
    const rows = [h("div", { class: "tl-axis" }, h("div"), ticks)];
    for (const s of ordered) {
      const claims = ((tl.segments || {})[s] || []).filter((c) => new Date(c.window.end).getTime() > t0);
      const track = h("div", { class: "tl-track" });
      for (const c of conflicts.filter((x) => x.segment_urn === s)) {
        track.append(h("div", {
          class: "band",
          style: { left: `${pct(c.window.start)}%`, width: `${Math.max(0.6, pct(c.window.end) - pct(c.window.start))}%` },
          title: `Conflict (${c.kind}): ${c.job_urns.map(short).join(" and ")}`,
        }));
      }
      // Overlapping claims go on separate lines (people above robots), so labels never collide.
      const lanes = { people: [], robot: [] };
      const placed = [];
      for (const c of [...claims].sort((a, b) => new Date(a.window.start) - new Date(b.window.start))) {
        const kind = String(c.actor_urn).startsWith("porter/") ? "people" : "robot";
        const start = new Date(c.window.start).getTime();
        let lane = lanes[kind].findIndex((end) => end <= start);
        if (lane === -1) { lane = lanes[kind].length; lanes[kind].push(0); }
        lanes[kind][lane] = new Date(c.window.end).getTime();
        placed.push({ c, kind, lane });
      }
      const LINE = 19;
      const peopleLines = Math.max(1, lanes.people.length);
      for (const { c, kind, lane } of placed) {
        const inferred = c.basis === "inferred";
        const left = pct(c.window.start);
        const width = Math.max(0.5, pct(c.window.end) - left);
        const top = 4 + (kind === "people" ? lane : peopleLines + lane) * LINE;
        track.append(h("div", {
          class: `claim ${kind}${inferred ? " inferred" : ""}`,
          style: { left: `${left}%`, width: `${width}%`, top: `${top}px`, opacity: 0.45 + 0.55 * c.confidence },
          title: `${short(c.job_urn)} ${time(c.window.start)}–${time(c.window.end)}, ${c.basis}, confidence ${c.confidence}`,
        }, `${short(c.job_urn)}${inferred ? `, inferred ${Math.round(c.confidence * 100)}%` : ""}`));
      }
      track.style.height = `${8 + (peopleLines + Math.max(1, lanes.robot.length)) * LINE}px`;
      track.append(h("div", { class: "now", style: { left: `${(PAST_S / (PAST_S + AHEAD_S)) * 100}%` } }));
      const kind = s.includes("lobby") ? "lobby" : s.includes("lift_") ? "lift" : "corridor";
      rows.push(h("div", { class: "tl-row" }, h("div", { class: "tl-label" }, seg(s), h("small", {}, kind)), track));
    }
    root.replaceChildren(...rows);
  }

  // ---------------------------------------------------------------- decisions

  const STATUS_CLASS = { awaiting_approval: "waiting", blocked: "blocked", failed: "failed", completed: "done", rejected: "blocked", expired: "blocked" };
  const PRODUCER = { fast_lane: "Playbook", solver: "Evaluator", agent: "Nemotron agent", operator: "Operator" };

  function renderPlans(root, plans, selected, onSelect) {
    if (!plans.length) { root.replaceChildren(h("li", { class: "empty" }, "No decisions yet. They appear as conflicts and incidents open.")); return; }
    root.replaceChildren(...plans.map((p) => {
      const d = p.decision || {};
      return h("li", { class: STATUS_CLASS[p.status] || "" },
        h("button", { "aria-current": p.plan_id === selected ? "true" : "false", onclick: () => onSelect(p.plan_id) },
          h("span", { class: "who" }, `${PRODUCER[p.producer] || p.producer}: ${p.status.replace("_", " ")}`),
          h("span", { class: "what" }, `${time(p.created_at)}, ${d.verdict || ""} ${(d.rule_ids || []).join(", ")}`)));
    }));
  }

  function describe(a) {
    switch (a.action) {
      case "close_lanes": return `Keep ${a.fleet_name} out of ${seg(a.segment_urn)}${a.window ? ` until ${time(a.window.end)}` : ""}`;
      case "open_lanes": return `Reopen ${seg(a.segment_urn)} for ${a.fleet_name}`;
      case "replan_robot": return `Replan ${a.robot_name}`;
      case "submit_task": return `Send ${a.fleet_name || "a robot"} from ${a.pickup_place || "its pickup"} to ${a.dropoff_place}${a.replaces_task_id ? ", replacing the current task" : ""}`;
      case "cancel_task": return `Cancel task ${a.task_id}`;
      case "interrupt_task": return `Hold the robot${a.window ? ` until ${time(a.window.end)}` : ""}`;
      case "resume_task": return "Resume the robot";
      case "notify": return `Tell ${a.recipient_urn}: "${a.message}"`;
      case "advise_staff": return `Advise ${a.recipient_urn}: "${a.message}"`;
      default: return a.action;
    }
  }

  function jobCard(j) {
    const job = j.job;
    const inferred = new Set(job.inferred_fields || []);
    const raw = j.raw || {};
    const interp = j.interpretation || {};
    // Hovering an inferred field shows the source text it was read from.
    const source = (name) => name === "destination" ? raw.destination_text : raw.notes;
    const why = (name) => `Inferred by Nemotron (confidence ${job.inference_confidence ?? interp.confidence ?? "?"}) from "${source(name) || "the job's fields"}"`;
    const field = (name, value) => [h("dt", {}, name), h("dd", {
      class: inferred.has(name) ? "inferred-field" : null, title: inferred.has(name) ? why(name) : null, tabindex: inferred.has(name) ? "0" : null,
    }, value ?? "–")];
    const constraints = interp.route_constraints || [];
    return h("div", { class: "job" },
      h("strong", {}, short(job.urn)), ` ${job.precedence.toUpperCase()}, ${job.status}`,
      h("dl", {},
        field("origin", job.origin),
        field("destination", job.destination),
        raw.destination_text ? [h("dt", {}, "as received"), h("dd", { class: "untrusted" }, `"${raw.destination_text}"`)] : null,
        raw.notes ? [h("dt", {}, "notes"), h("dd", { class: "untrusted" }, `"${raw.notes}"`)] : null,
        constraints.length ? [h("dt", {}, "route"), h("dd", { class: "inferred-field", title: why("route"), tabindex: "0" }, constraints.join("; "))] : null,
        field("due", job.due_at ? time(job.due_at) : null),
        job.bed_type ? field("bed", job.bed_type) : null,
        job.payload ? field("payload", job.payload) : null,
        interp.inferred ? [h("dt", {}, "reading"), h("dd", {}, interp.rationale || "")] : null));
  }

  function traceList(decisions) {
    const steps = decisions.flatMap((d) => d.trace || []);
    if (!steps.length) return null;
    return [h("h3", {}, "Reasoning trace"), h("ol", { class: "trace" }, steps.map((s) => s.kind === "llm"
      ? h("li", { class: `llm ${s.outcome}` }, h("span", { class: "tool" }, `${s.task} (${s.role === "reason" ? "Super" : "Nano"})`),
          ` ${s.model_id}, ${s.latency_ms} ms, ${s.prompt_tokens + s.completion_tokens} tokens, ${s.outcome}${s.error ? `: ${s.error}` : ""}`)
      : h("li", {}, h("span", { class: "tool" }, s.tool), " ", JSON.stringify(s.args),
          h("pre", {}, JSON.stringify(s.result).slice(0, 1200)))))];
  }

  function optionsTable(decisions, chosenPlan) {
    const withOptions = decisions.filter((d) => (d.options || []).length);
    if (!withOptions.length) return null;
    const d = withOptions[withOptions.length - 1];
    return [h("h3", {}, "Options the evaluator scored"),
      h("div", { class: "scroll-x" }, h("table", {}, h("thead", {}, h("tr", {}, ["Option", "Actions", "Robot ETA", "Late by", "Delay to people", "Score"].map((t) => h("th", {}, t)))),
        h("tbody", {}, d.options.map((o) => {
          const robot = o.effects[0] || {};
          const same = JSON.stringify(o.actions.map((a) => a.action)) === JSON.stringify(chosenPlan.actions.filter((a) => a.action !== "notify").map((a) => a.action));
          return h("tr", { class: same ? "chosen" : null },
            h("td", {}, o.template.replace("_", " ")),
            h("td", {}, o.actions.map((a) => a.action).join(", ") || "none"),
            h("td", {}, time(robot.eta)),
            h("td", {}, `${Math.round(robot.lateness_s || 0)} s`),
            h("td", {}, `${Math.round(o.delay_to_people_s || 0)} s`),
            h("td", {}, o.score.join(" / ")));
        })))),
      d.note ? h("p", { class: "note" }, d.note) : null];
  }

  function verificationText(p) {
    const v = p.verification;
    if (v.passed === false) return `Failed, undone where possible: ${v.reason}`;
    const checks = (v.checks || []).map((c) => `${c.condition}: ${c.outcome} (${c.detail})`).join("; ");
    if (v.passed === true) return checks ? `Verified against live state. ${checks}` : "Verified: every command confirmed (lane closures read back from Open-RMF).";
    return "Checking the post-conditions against live state.";
  }

  // opts.onDecide(planId, verb): approve/reject buttons (live: the API; replay: the branch)
  function renderDetail(root, ctx, opts = {}) {
    const p = ctx.plan;
    const plan = p.plan;
    const d = p.decision || {};
    const buttons = p.status === "awaiting_approval" && opts.onDecide ? h("div", { class: "actions-row" },
      h("button", { class: "approve", onclick: () => opts.onDecide(p.plan_id, "approve") }, "Approve"),
      h("button", { class: "reject", onclick: () => opts.onDecide(p.plan_id, "reject") }, "Reject"),
      opts.decideNote ? h("span", { class: "note" }, opts.decideNote) : null) : null;
    const approval = p.approval ? h("p", { class: "note" },
      `${p.approval.approved ? "Approved" : "Rejected"} by ${p.approval.by}${p.approval.reason ? `: ${p.approval.reason}` : ""}`) : null;
    root.replaceChildren(...nodes(
      h("div", { class: "head" }, h("h3", {}, PRODUCER[p.producer] || p.producer),
        d.verdict ? h("span", { class: `pill ${d.verdict}` }, d.verdict) : null, h("span", { class: "note" },
          `${(d.rule_ids || []).join(", ") || "no rule triggered"}; ${d.approval_route === "person" ? "a person decides" : "automatic"}; ${p.status.replace("_", " ")}${ctx.decision_latency_ms != null ? `; decided in ${Math.round(ctx.decision_latency_ms)} ms` : ""}`)),
      h("p", { class: "rationale" }, plan.rationale || ""),
      buttons,
      approval,
      h("h3", {}, "What it does"),
      plan.actions.length ? h("ul", {}, plan.actions.map((a) => h("li", {}, describe(a)))) : h("p", { class: "note" }, "Nothing: no action is needed."),
      ctx.jobs.length ? [h("h3", {}, "Jobs, as received and as interpreted"), h("div", { class: "jobs" }, ctx.jobs.map(jobCard))] : null,
      optionsTable(ctx.decisions, plan),
      ctx.evidence.length ? [h("h3", {}, "Evidence"), ctx.evidence.map((e) => h("div", { class: "evidence" },
        h("a", { href: e.url, target: "_blank", rel: "noopener noreferrer" }, e.url), " ",
        h("span", { class: "untrusted" }, "(untrusted source)"),
        h("p", {}, e.summary),
        e.extracted ? h("p", {}, h("strong", {}, e.extracted.fault_code), `: ${e.extracted.meaning} Recovery ${e.extracted.expected_recovery_s ?? "unknown"} s; technician ${e.extracted.requires_manual_reset ? "required" : "not required"}.`) : null))] : null,
      traceList(ctx.decisions),
      p.verification ? [h("h3", {}, "Verification"), h("p", {}, verificationText(p))] : null,
      h("h3", {}, "Commands"),
      h("table", {}, h("tbody", {}, (p.commands || []).map((c) => h("tr", {},
        h("td", {}, c.action), h("td", {}, c.status), h("td", {}, c.not_before ? `at ${time(c.not_before)}` : ""),
        h("td", {}, c.inverse_of ? "undo" : "")))))));
  }

  // ---------------------------------------------------------------- incidents

  // opts.onEvidence(ref): a cause's evidence reference was clicked (replay: open the entry)
  function renderIncidents(root, inc, opts = {}) {
    const open = inc.open.map((i) => ({ ...i, open: true }));
    const openIds = new Set(open.map((i) => i.incident_id));
    const all = [...open, ...inc.recent.filter((i) => !openIds.has(i.incident_id)).slice(0, 8)];
    if (!all.length) { root.replaceChildren(h("li", { class: "empty resolved" }, "No stalls.")); return; }
    root.replaceChildren(...all.map((i) => h("li", { class: i.open ? "" : "resolved" },
      h("strong", {}, `${i.robot_urn.replace("robot/", "")}: ${i.trigger.replace("_", " ")}`),
      ` at ${time(i.detected_at)}, ${i.stakes.precedence}${i.stakes.payload ? ` ${i.stakes.payload}` : ""}${i.stakes.slack_s != null ? `, slack ${Math.round(i.stakes.slack_s)} s` : ""}${i.open ? "" : `, ${i.status}`}`,
      i.report ? [h("p", {}, i.report.summary), h("ul", { class: "causes" }, i.report.causes.map((c) => h("li", {},
        `Possible, unconfirmed: ${c.description.replace(/^Possible:\s*/, "")}`,
        (c.evidence_refs || []).length ? [" ", (c.evidence_refs || []).map((ref, n) => opts.onEvidence
          ? h("button", { class: "link ref", onclick: () => opts.onEvidence(ref) }, `evidence ${n + 1}`)
          : h("code", { class: "ref", title: ref }, ref.slice(0, 8)))] : null)))] : null)));
  }

  // ---------------------------------------------------------------- map (live: both levels)

  function renderMap(root, m) {
    const fleet = m.fleets.deliveryRobot || Object.values(m.fleets)[0];
    if (!fleet) return;
    const levels = [...new Set(fleet.vertices.map((v) => v.level))].sort().reverse();
    const box = {};
    for (const l of levels) {
      const vs = fleet.vertices.filter((v) => v.level === l);
      box[l] = { x0: Math.min(...vs.map((v) => v.x)), x1: Math.max(...vs.map((v) => v.x)), y0: Math.min(...vs.map((v) => v.y)), y1: Math.max(...vs.map((v) => v.y)) };
    }
    const gap = 6;
    let offset = 0;
    const shift = {};
    for (const l of levels) { shift[l] = offset - box[l].x0 + 3; offset += box[l].x1 - box[l].x0 + gap; }
    const height = Math.max(...levels.map((l) => box[l].y1 - box[l].y0)) + 10;
    const yTop = Math.max(...levels.map((l) => box[l].y1)) + 6;
    const X = (l, x) => x + shift[l];
    const Y = (y) => yTop - y;
    const out = svg("svg", { viewBox: `0 0 ${offset} ${height}`, role: "img", "aria-label": "Floor plans with robots and closed lanes" });
    for (const l of levels) out.append(svg("text", { x: X(l, box[l].x0), y: 4, class: "level-label" }, l));
    for (const [fname, f] of Object.entries(m.fleets)) {
      const closed = new Set(f.closed);
      for (const [a, b, i] of f.lanes) {
        const va = f.vertices[a], vb = f.vertices[b];
        if (!va || !vb || va.level !== vb.level || !Object.hasOwn(shift, va.level)) continue;
        if (fname !== "deliveryRobot" && !closed.has(i)) continue;
        out.append(svg("line", { x1: X(va.level, va.x), y1: Y(va.y), x2: X(vb.level, vb.x), y2: Y(vb.y), class: closed.has(i) ? "lane closed" : "lane" }));
      }
    }
    for (const [urn, s] of Object.entries(m.segments)) {
      if (s.kind !== "lift") continue;
      const name = urn.replace("segment/", "");
      const lift = m.lifts[name] || {};
      const labelled = new Set();
      for (const i of (s.lanes.deliveryRobot || [])) {
        const lane = fleet.lanes.find((x) => x[2] === i);
        const v = lane && fleet.vertices[lane[1]];
        if (!v || !Object.hasOwn(shift, v.level)) continue;
        out.append(svg("rect", { x: X(v.level, v.x) - 1.2, y: Y(v.y) - 1.2, width: 2.4, height: 2.4, class: `lift${lift.mode && lift.mode !== "AGV" ? " down" : ""}` },
          svg("title", {}, `${name}: ${lift.mode || "unknown"} ${lift.fault_code || ""}`)));
        if (!labelled.has(v.level)) {
          labelled.add(v.level);
          out.append(svg("text", { x: X(v.level, v.x) + 2, y: Y(v.y) + 0.8, class: "robot-label" }, name));
        }
      }
    }
    for (const r of m.robots) {
      if (!Object.hasOwn(shift, r.level)) continue;
      out.append(svg("circle", { cx: X(r.level, r.x), cy: Y(r.y), r: 1.1, class: "robot" }, svg("title", {}, `${r.fleet}/${r.name} ${r.task_id || "idle"}`)));
      out.append(svg("text", { x: X(r.level, r.x) + 1.5, y: Y(r.y) + 0.8, class: "robot-label" }, r.name.replace("deliveryRobot_", "D").replace("tinyRobot_", "T")));
    }
    root.replaceChildren(out);
    const down = Object.entries(m.lifts).filter(([, l]) => l.mode && l.mode !== "AGV").map(([n, l]) => `${n} ${l.mode} ${l.fault_code || ""}`);
    return down.length ? `Out of service: ${down.join(", ")}` : "All lifts in service";
  }

  return { h, svg, nodes, clock, time, short, seg, PRODUCER, renderTimeline, renderPlans, renderDetail, renderIncidents, renderMap };
})();
