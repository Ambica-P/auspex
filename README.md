# Auspex

**Critical patients first: reasoning-based traffic control for hospital AMR fleets.**

Auspex is a coordination layer above [Open-RMF](https://www.open-rmf.org/). It reads what a hospital already knows about who is moving where, and how urgently. It turns that into live, precedence-aware decisions for the autonomous mobile robot (AMR) fleet. It uses NVIDIA Nemotron on Nebius Token Factory for the cases where that information is ambiguous, incomplete or conflicting.

- **Live site: [auspex-theta.vercel.app](https://auspex-theta.vercel.app/)**: recorded runs on a 2D map, a live Nemotron Playground, the benchmark and a verifiable audit ledger
- **Track:** Nebius x NVIDIA Global AI Hackathon, Physical AI

> Open-RMF moves the robots. Auspex makes sure they move around the people who matter most.

---

## The problem

A hospital runs two transport systems that never talk to each other:

- **Porters.** They move patients, beds and equipment, dispatched by an EHR transport module such as Epic Transport or TeleTracking.
- **Delivery robots.** They are coordinated by Open-RMF.

Both compete for the same corridors, doors and lifts. Open-RMF can't see either of the things that should decide who goes first:

- **Patient transports.** Open-RMF has no deadlines and only a binary priority.
- **Clinical urgency.** The hospital's own data says how urgent a move is, but that data is messy: free-text destinations, constraints hidden in notes, statuses that arrive out of order.

**Reference scenario:**

- **Alex**, a nurse, needs oxygen delivered to the patient in bed 12 by 10:40.
- **Jane** supplies the delivery robots, which run on Open-RMF.
- **Rohan** supplies the bedside arm that takes the tank from the robot.
- **A porter** is moving a patient on a bariatric bed to imaging, through the only lift that fits the bed. That is the same lift the oxygen robot is about to take.

The transport system recorded the move as destination `"CT 2"`, with notes `"on portable vent, needs large car"`. No structured field says which lift it needs.

**Practitioner input.** This comes from a robotics engineer working on hospital robot deployments, anonymised:

- Human and bed obstructions near lifts are common.
- Open-RMF does not plan around external obstacles.
- Robots report "blocked" without saying why.
- How much a delay matters has to come from the hospital's workflow.
- Dynamic lane closure is a practical lever.
- Patient and bed transports are rarely tracked in real time.

## How Auspex decides

1. **Hospital policy decides precedence.** The order of clinical classes (CODE, STAT, CRITICAL and so on) is YAML authored by the hospital (`policy/`). Auspex never infers clinical urgency.
2. **Arithmetic is deterministic.** Lift and corridor claims, ETAs, conflict detection, option scoring and policy checks are plain code.
3. **Nemotron handles ambiguity, uncertainty and trade-offs.** It reads messy transport records into typed fields, and estimates how long a fault will last from vendor evidence. When no rule-based option meets every deadline, it proposes composite plans. It also explains its plans and writes incident reports.
4. **The gate checks every plan.** The policy gate scores every proposal again, and every proposal needs ALLOW (and approval where policy requires it) before anything happens.
5. **The LLM never moves a robot.** Model output is a typed `Plan`. Only the adapter acts, after the gate and approval. `tests/` proves that no code path skips the gate.
6. **Clinical ties go to a person.** Ordering two STAT patients is routed to a nurse for approval.
7. **Verify, record, explain.** Every fleet change is written to a SHA-256 hash-chained ledger before it is reported. An action counts as done only when its post-conditions hold against live state.

It works in two modes:

- **Proactive:** forecast lift and corridor use about 15 minutes ahead, and resolve conflicts before the robot arrives.
- **Reactive:** detect a robot stalling for no explained reason, judge what's at stake (deadline, payload), act, and afterwards write an incident report. Causes in the report are listed as possible, never asserted.

Containment, the fast lane, is deterministic: no LLM or search call, with a 2-second budget.

## Where NVIDIA, Nebius and Tavily are used

| Component | Where | What it does |
|---|---|---|
| **Nemotron Nano** `nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B` | `src/auspex/agent/agent.py` (`interpret`, `extract`, `report`) | Reads free-text transport records into typed fields: destination alias, route constraints, confidence. Extracts typed facts from vendor bulletins (fault meaning, recovery time, manual reset). Writes incident reports. |
| **Nemotron Super** `nvidia/nemotron-3-super-120b-a12b` | `agent.py` (`outage`, `choose`, `tradeoff`) | Weighs waiting against rerouting or handover under uncertainty. Proposes composite options when no template meets every deadline. Explains plans that go to a person. |
| **Nebius Token Factory** | `src/auspex/agent/llm.py` | OpenAI-compatible endpoint for both models: JSON-schema structured output, one repair retry, then it fails closed to the deterministic best option. |
| **Tavily** | `src/auspex/evidence/` | When a lift reports a fault code, Auspex searches the lift vendor's advisories, restricted to the vendor's domain. When search finds nothing, it reads the vendor's bulletin index with Tavily Extract. Results are untrusted: they are kept separate from the instructions, never followed, and only validated typed fields reach a plan. |
| **Playground backend** | `sandbox/` | Calls Nemotron Nano on Token Factory live from the judge site, on a recorded S1 snapshot, with no ROS or database. The container (`sandbox/Dockerfile`) runs on Render; usage counters are in Upstash Redis. |

Nebius Serverless Jobs was cut for time: the benchmark ran locally. The Playground container is hosted on Render rather than a Nebius Serverless Endpoint for the same reason; it runs unchanged on either.

## Scenarios

All data is synthetic. The hospital mock is shaped like real transport systems (Epic Transport, TeleTracking). The scenario set is **deliberately weighted toward edge cases** where the right action cannot be computed from structured fields alone. Each scenario in `sim/scenarios/` carries its own ground truth.

| | Scenario | What it needs |
|---|---|---|
| **S1** | A bariatric-bed transport to `"CT 2"`, with the lift constraint only in the notes | Nemotron interprets the record. Auspex closes `lift_25` to the robot fleet for that window, and the oxygen robot switches to `lift_1` mid-task. |
| **S2** | The oxygen robot stalls at a lift reporting fault **E-17** | Tavily finds the vendor bulletin (auto-recovers in about 2 min). Nemotron weighs waiting against handover. An incident report follows. Variant **E-42** (technician required) must lead to a handover. |
| **S3** | Two STAT transports and the oxygen robot share one working lift | Precedence orders the ED transport first. Nemotron proposes composite options, and the gate routes the clinical tie to a person. |

## Benchmark

The modes are compared on the same scenario scripts:

- **A:** Open-RMF alone (Auspex only observes).
- **B:** Auspex, rules only (forecasting, fast lane, evaluator, gate; free text stays uninterpreted).
- **C:** Auspex with Nemotron.

The measured table is in [`bench/results/table.md`](bench/results/table.md), with run counts. It is regenerated by `scripts/record-replays` or `scripts/run-bench`, and shown on the live site's Benchmark page. These are **results on the edge-case set**, not general fleet performance.

## Architecture

```
Hospital mock (transport jobs, delivery requests) ──┐
Open-RMF ROS 2 topics ──▶ ros_bridge (rclpy) ───────┼─▶ Postgres event queue (LISTEN/NOTIFY)
                                                    │
CORE (FastAPI, Python 3.12)                         ▼
  Operational picture ─▶ Forecaster ─▶ Resource timeline ─▶ Conflict detector     proactive
  Progress monitor ─▶ Incident detector                                           reactive
  Decision engine: fast lane (YAML, ≤2 s) · evaluator + option templates
                   · Nemotron Nano/Super on Token Factory, Tavily evidence
  Policy gate ─▶ Approval ─▶ Adapter ─▶ Verifier ─▶ Hash-chained ledger
        │ REST / SSE                         │ lane closures, replans, task API
  Ops console (ui/)                    Open-RMF ─▶ robots, doors, lifts

Recorded runs ─▶ web/ (Next.js on Vercel) ──/api/playground──▶ sandbox/ (FastAPI on Render) ──▶ Nemotron Nano
                                                                      └─ usage counters: Upstash Redis
```

**Stack:**

- Open-RMF on ROS 2 Kilted, with the `rmf_demos` clinic world.
- Python 3.12: FastAPI, Pydantic v2, SQLAlchemy and Alembic on Postgres 16, structlog, the `openai` SDK (pointed at Token Factory), `tavily-python`, uv.
- Next.js 16 and React 19 for the judge site, on Vercel; vanilla JS and SVG for the ops console.
- Playground backend: the same Python packages in a Docker container on Render, with Upstash Redis for usage counters.

| Path | Contents |
|---|---|
| `src/auspex/` | The core: picture, forecast, monitor, decide, agent, evidence, policy, execute, verify, ledger, api |
| `ros_bridge/` | The only process that imports `rclpy`: topics → events, ROS-side commands |
| `rmf_adapter_ext/` | Fleet adapter extension: a replan service for a named robot |
| `hospital_mock/`, `sim/` | Synthetic hospital systems, scenario controller, scenarios with ground truth, the fictional vendor advisory site |
| `policy/`, `playbooks/`, `site/` | Hospital-authored precedence and rules, fast-lane playbooks, site model (lift attributes, routes, zone aliases) |
| `bench/` | A/B/C benchmark report |
| `ui/` | Ops console, served by the core at `/ui/` |
| `web/` | Judge site: simulation replays, Playground, benchmark, audit ledger |
| `sandbox/` | Playground backend (FastAPI container): S1 snapshot, limits, usage counters |

---

## Run it locally (WSL2, from clone to demo)

Tested on Windows 11 with WSL2 running Ubuntu 24.04. Native Ubuntu 24.04 should also work. Every command is bash inside WSL. Each long-running process needs its own terminal.

### Prerequisites

- **ROS 2 Kilted** and **Open-RMF** binaries: `sudo apt install ros-kilted-rmf-dev` (0.2.0).
- **`rmf_demos`** built from source in `~/rmf_ws`, at the version matching Kilted. It provides the clinic world.
- **Docker Engine inside WSL2** with the compose plugin, for Postgres and the Open-RMF API server.
- **uv**: `curl -LsSf https://astral.sh/uv/install.sh | sh`. It provides Python 3.12 for the core.
- **Node 20+ installed inside WSL** (only for `web/`). Use the Linux build, not the Windows one: in WSL, `which npm` must print a Linux path, not `/mnt/c/...`.
  ```bash
  curl -o- https://raw.githubusercontent.com/nvm-sh/nvm/v0.40.3/install.sh | bash
  source ~/.bashrc && nvm install 22 && which node npm
  ```
- **Memory.** 16 GB RAM is recommended. On WSL, cap the VM in `%UserProfile%\.wslconfig` (for example `memory=8GB`, `swap=8GB`). The simulation runs headless by default.
- **Larger UDP buffers**, set once. Without them the building map gets lost over DDS and no robot registers; `scripts/run-sim` checks for this.
  ```bash
  printf 'net.core.rmem_max=8388608\nnet.core.wmem_max=8388608\nnet.core.rmem_default=8388608\nnet.core.wmem_default=8388608\n' | sudo tee /etc/sysctl.d/60-auspex-dds.conf && sudo sysctl --system
  ```
- **Optional:** a Nebius Token Factory API key and a Tavily API key. Without them, a deterministic stand-in model and local evidence are used.

### 1. Clone, configure, bootstrap

```bash
git clone https://github.com/ambica-p/auspex.git
cd auspex
cp .env.example .env      # set POSTGRES_PASSWORD, AUSPEX_APP_DB_PASSWORD (also inside the two URLs), ADMIN_TOKEN
scripts/bootstrap         # core venv (uv) + ROS bridge venv
scripts/check-env
```

Never commit `.env`. It is in `.gitignore`.

### 2. Database

```bash
scripts/db-up             # Postgres 16 in Docker
scripts/migrate           # roles (append-only ledger role) and tables
```

### 3. Open-RMF API server (terminal 1)

```bash
docker run --network host -it --rm ghcr.io/open-rmf/rmf-web/api-server:kilted-nightly
```

### 4. Simulation (terminal 2)

Headless is the default: no Gazebo or RViz windows, lowest memory.

```bash
scripts/run-sim
```

To **watch the robots in Gazebo and RViz**, use the line below instead. It needs WSLg (Windows 11) or an X server, and noticeably more memory. If WSL runs out of memory, go back to headless.

```bash
scripts/run-sim --gui
```

### 5. ROS bridge (terminal 3)

```bash
scripts/run-bridge
```

### 6. Core (terminal 4)

Capture the nav graphs once, with the sim running. They are saved to `site/nav_graphs/`. Then start the core:

```bash
scripts/capture-navgraphs
scripts/run-core          # REST API + ops console on http://127.0.0.1:8100 (CORE_PORT)
scripts/run-ui            # prints the ops console URL; open it in a Windows browser, paste ADMIN_TOKEN
```

### 7. Run the scenarios (terminal 5)

```bash
scripts/run-scenario s1 --config A     # baseline: the robot waits at the lift
scripts/run-scenario s1 --config C     # Auspex + Nemotron
scripts/run-scenario s2 --config C
scripts/run-scenario s2_e42 --config C
scripts/run-scenario s3 --config C
```

Each run prints PASS or FAIL against the scenario's ground truth. `--config B` runs Auspex with rules only.

### 8. Switch to Nemotron and Tavily

In `.env`:

- set `LLM_PROVIDER=tokenfactory`, `NEBIUS_API_BASE`, `NEBIUS_API_KEY`, `MODEL_TRIAGE` (Nano) and `MODEL_REASON` (Super). Copy the base URL and model IDs from the Nebius console.
- set `SEARCH_PROVIDER=tavily`, `TAVILY_API_KEY`, and `VENDOR_SITE_URL`. That URL points at a published copy of `sim/vendor_site/`, for example on GitHub Pages.

Restart `run-core`, then check:

```bash
scripts/check-llm         # both models answer in the expected schema
scripts/check-evidence --provider tavily
```

### 9. Benchmark and recordings

```bash
scripts/run-bench --configs A,B,C --runs 1   # writes bench/results/table.md
scripts/record-replays                       # records every scenario × mode for the site, then the table
scripts/build-web-data                       # run index + site.json into web/public/, secret scan
scripts/test                                 # ruff, mypy, tests
```

### 10. The judge site

The site needs only the recordings in `web/public/`, not the stack above, and not `run-core`.

```bash
cd web
npm install
npm run dev               # http://127.0.0.1:8090/
```

**Playground backend** (Python only, no ROS or database): run it from the repo root in another terminal, then restart `npm run dev`.

```bash
scripts/run-sandbox --check     # the four example inputs, once
scripts/run-sandbox             # http://127.0.0.1:8095
echo "SANDBOX_URL=http://127.0.0.1:8095" > web/.env.local
```

Real Nemotron in the Playground needs `SANDBOX_PRICE_IN_PER_MTOK` and `SANDBOX_PRICE_OUT_PER_MTOK` in `.env`: Nano's prices from the Nebius console. They enforce the spend cap `SANDBOX_BUDGET_USD`. Use `--fake` to run without a key. Optionally set `UPSTASH_REDIS_REST_URL` and `UPSTASH_REDIS_REST_TOKEN` to keep the counters in Upstash (otherwise they live in memory). Local runs and the deployed backend then share the same budget.

### Deploying

- **Site:** import the repository into Vercel with **Root Directory `web`**. Set `SANDBOX_URL` (server-side only) to the sandbox's URL, or leave it unset; the Playground then shows the recorded answer.
- **Playground backend (Render, free plan):** New → Web Service from the repo, language **Docker**, Dockerfile path `sandbox/Dockerfile`, build context `.`, health check path `/health`. Environment variables (values without `$`):
  - `PORT=8000`, `SANDBOX_ENABLED=true`;
  - `NEBIUS_API_BASE`, `MODEL_TRIAGE`, and a Token Factory key used **only** by the sandbox as `NEBIUS_API_KEY`;
  - `SANDBOX_BUDGET_USD`, `SANDBOX_PRICE_IN_PER_MTOK`, `SANDBOX_PRICE_OUT_PER_MTOK`, `SANDBOX_DAILY_CAP`, `SANDBOX_RATE_PER_IP_PER_MIN`;
  - `UPSTASH_REDIS_REST_URL` and `UPSTASH_REDIS_REST_TOKEN` from a free Upstash Redis database, so the spend and daily counters survive Render restarting a sleeping service.

  Then set `SANDBOX_URL` in Vercel to the Render URL and redeploy. The same container also runs on a Nebius Serverless Endpoint or any Docker host.
- **Limits:** every Nemotron call reserves its estimated cost against `SANDBOX_BUDGET_USD` first. When the budget, the daily cap or the per-IP rate is reached, or the counter store can't be reached, no model call is made and the site shows the recorded S1 decision with the reason.

---

## Known limitations

- All data is synthetic, and the scenarios are edge cases by design. The benchmark describes behaviour on that set only.
- The judge site replays recorded runs. The ROS 2 and Open-RMF pipeline is not hosted, because concurrent visitors would share one simulation state.
- Only tested on WSL2, Ubuntu 24.04, ROS 2 Kilted and `ros-kilted-rmf-dev` 0.2.0, in the `rmf_demos` clinic world.
- The robot ETA model is approximate. Robot claims are widened (20 s plus 15% of the lead time) so lift conflicts aren't missed.
- Nemotron runs with its reasoning mode off, to keep structured answers within budget. "Technician required" is also enforced from the vendor bulletin's typed `requires_manual_reset` field rather than left to the model.
- The Playground's per-IP rate limit is kept in memory and trusts the forwarded client address; the spend budget and the daily cap are persistent (Upstash) and apply regardless. On Render's free plan the first request after an idle period can time out while the service wakes; the site then shows the recorded answer.
- The Token Factory Serverless model IDs above are scheduled for replacement on 9 Nov 2026 (`nvidia/Nemotron-3_5-Lightning`, `nvidia/Nemotron-3.5-Super-VL`). Model IDs are configuration, not code.
- Nebius Serverless Jobs for the benchmark batch was not built.

## Related work

- **Integration and workflow:** RoMi-H and integrator platforms; Node-RED orchestration on Open-RMF Dynamic Events.
- **Scheduling:** flight-aware cleaning schedulers at airports.
- **Operations views:** rmf-web dashboards; porter dispatch systems; hospital command centres such as Qventus and GE HealthCare.
- **LLM multi-robot replanning:** research systems such as CoMuRoS and FLEET. The credible designs pair the LLM with a formal evaluator, the pattern Auspex follows.

We found no publicly documented system that joins human transport demand with live, precedence-aware robot coordination.

## Where this goes next (startup direction)

- **Integration levels per site:** observation only (reactive mode everywhere); then transport and RTLS feeds (proactive); then Auspex's own history (hot-spot-aware planning).
- **System integrators** as the route to market. Integrators map each site's data (including RTLS to RMF map alignment) and deploy it.
- **Learning from the ledger:** calibrate ETAs and route durations from forecast-versus-actual records; promote playbooks with human sign-off; later, fine-tune Nemotron on recorded decisions.
- **Change governance:** firmware and map changes checked against vendor release notes (Tavily), rehearsed in simulation, rolled out as canaries.

## Licence and third-party notices

Auspex is licensed under Apache-2.0 (see the repository's licence). It builds on:

- Open-RMF, `rmf_demos` and rmf-web (Apache-2.0); ROS 2 (Apache-2.0);
- FastAPI, Pydantic, SQLAlchemy, Alembic, structlog, uv (MIT/BSD);
- Next.js and React (MIT); the Geist font (SIL OFL 1.1);
- the `openai` and `tavily-python` SDKs (MIT).

NVIDIA Nemotron models are used through Nebius Token Factory under their respective model licences. All people, vendors (Jane Robotics, Ascendia Lifts, Rohan Bedside) and hospital data in this repository are fictional.