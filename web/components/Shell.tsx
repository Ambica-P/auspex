"use client";

// The site's markup. The replay engine (lib/replay-engine.js) fills and animates it; React owns
// the layout and the collapsible sidebar.
import { useEffect, useRef, useState } from "react";

// 24x24 stroke icons (path data only)
const ICONS: Record<string, string> = {
  replay: "M8 5v14l11-7z",
  try: "M4 17l6-6-6-6M12 19h8",
  results: "M4 20V10M10 20V4M16 20v-7M22 20H2",
  ledger: "M9 12l2 2 4-4M12 3l8 4v5c0 5-3.5 8-8 9-4.5-1-8-4-8-9V7z",
  setup: "M4 4h16v12H4zM8 20h8M12 16v4",
  usage: "M12 3a9 9 0 1 0 9 9M12 12l6-6M21 3v6h-6",
};

const NAV: { view: string; label: string; hint: string }[] = [
  { view: "replay", label: "Simulation", hint: "Recorded runs on the 2D map" },
  { view: "try", label: "Playground", hint: "Call Nemotron from your browser" },
  { view: "results", label: "Benchmark", hint: "A vs B vs C, measured" },
  { view: "ledger", label: "Audit ledger", hint: "Hash-chained log, verify or tamper" },
  { view: "setup", label: "Self-host", hint: "Run the full stack on your machine" },
  { view: "usage", label: "Rate limits", hint: "Tokens, budget, caps" },
];

const REPO_URL = "https://github.com/ambica-p/auspex";
const SIDEBAR_KEY = "auspex.sidebar";
let started = false; // React strict mode mounts twice in development

function PanelIcon() {
  return (
    <svg width="20" height="20" viewBox="0 0 24 24" aria-hidden="true" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <rect x="3" y="4.5" width="18" height="15" rx="4" />
      <line x1="9" y1="5" x2="9" y2="19" />
    </svg>
  );
}

export default function Shell() {
  const [collapsed, setCollapsed] = useState(false);
  const ready = useRef(false);

  useEffect(() => {
    try { setCollapsed(localStorage.getItem(SIDEBAR_KEY) === "collapsed"); } catch { /* private window */ }
    if (started) return;
    started = true;
    import("@/lib/replay-engine").then((m) => { m.initReplay(); ready.current = true; });
  }, []);

  const toggle = () => {
    setCollapsed((c) => {
      try { localStorage.setItem(SIDEBAR_KEY, c ? "open" : "collapsed"); } catch { /* ignore */ }
      return !c;
    });
  };

  return (
    <>
      <header className="topbar">
        <button className="side-toggle" onClick={toggle} aria-label={collapsed ? "Open sidebar" : "Close sidebar"} aria-expanded={!collapsed} title={collapsed ? "Open sidebar" : "Close sidebar"}>
          <PanelIcon />
        </button>
        <div className="brand"><img className="logo" src="/assets/logo.png" alt="" width={28} height={28} /><span className="brand-name">Auspex</span></div>
        <span className="topbar-tag">Physical AI · Nemotron on Nebius</span>
        <div className="topbar-right">
          <a id="repo-link" className="repo" href={REPO_URL} target="_blank" rel="noopener noreferrer">Source on GitHub</a>
        </div>
      </header>
    <div className={`shell${collapsed ? " collapsed" : ""}`}>
      <aside className="sidebar" aria-label="Sections">
        <nav className="side-nav">
          {NAV.map((n, i) => (
            <button key={n.view} data-view={n.view} aria-pressed={i === 0} title={n.hint}>
              <svg className="nav-icon" viewBox="0 0 24 24" aria-hidden="true" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round"><path d={ICONS[n.view]} /></svg>
              <span className="nav-text"><span className="nav-label">{n.label}</span>
              <span className="nav-hint">{n.hint}</span></span>
            </button>
          ))}
        </nav>
        <p className="side-note">Recorded runs of a simulated clinic. All people and data are fictional.</p>
      </aside>

      <div className="content">
        {/* ======================================================== simulation */}
        <main className="replay-main" id="replay-view">
          <header className="headline">
            <h1>Critical patients first: reasoning-based traffic control for hospital AMR fleets.</h1>
            <p>Auspex sits above Open-RMF. When an autonomous mobile robot and a patient transport share the same environment, it determines who gets priority and steers the robot.</p>
          </header>

          <section className="player" aria-label="Simulation">
            <div className="picker">
            <p className="replay-banner inline" role="note">Replays of recorded runs from a local simulation. Self-host runs the live system.</p>
              <div className="pick-row">
                <span className="pick-label">Scenario</span>
                <div id="scenarios" className="choices" role="group" aria-label="Scenario" />
              </div>
              <p id="scenario-blurb" className="blurb" />
              <div className="pick-row">
                <span className="pick-label">Mode</span>
                <div id="configs" className="configs" role="group" aria-label="Configuration" />
              </div>
              <div className="options-row">
                <label><input type="checkbox" id="compare" /> <span id="compare-label">Side by side with A</span></label>
                <span id="branch" className="note" />
                <div className="floors" role="group" aria-label="Floor" id="floors" />
              </div>
            </div>
            <div className="stage">
              <div id="maps" className="maps" />
              <aside className="story" aria-live="polite" aria-labelledby="story-title">
                <h3 id="story-title">What&apos;s happening</h3>
                <p id="story-now" className="story-now" />
                <ol id="story-steps" className="story-steps" />
                <p className="story-key"><span className="dot robot" />AMR <span className="dot person" />patient <span className="box" />lift <span className="line" />closed to AMRs</p>
              </aside>
            </div>
            <div id="prompt" className="prompt" hidden />
            <div className="playback">
              <button id="play" className="icon" aria-label="Play">Play</button>
              <button id="back" className="icon" aria-label="Back one second">−1 s</button>
              <button id="fwd" className="icon" aria-label="Forward one second">+1 s</button>
              <label className="speed">Speed{" "}
                <select id="speed" defaultValue="4">
                  <option value="0.5">0.5x</option><option value="1">1x</option><option value="2">2x</option><option value="4">4x</option>
                </select>
              </label>
              <span id="clock" className="clock" aria-live="off" />
              <div className="scrub">
                <div id="markers" className="markers" />
                <input id="scrubber" type="range" min="0" max="1000" defaultValue="0" step="1" aria-label="Playback position" />
              </div>
            </div>
            <p className="speed-note">Plays at 4x. Change Speed or drag the bar anytime.</p>
            <div className="legend markers-legend" id="marker-legend" />
            <p id="run-status" className="note" />
          </section>

          <section className="timeline-panel" aria-labelledby="tl-title">
            <div className="panel-head">
              <h2 id="tl-title">Lift bookings, next 12 minutes</h2>
              <div className="legend">
                <span className="key people">Patients</span>
                <span className="key people inferred">Patients, read by Nemotron</span>
                <span className="key robots">AMRs</span>
                <span className="key conflict">Clash</span>
              </div>
            </div>
            <div id="timeline" className="timeline" />
          </section>

          <section className="decisions" aria-labelledby="dec-title">
            <div className="panel-head"><h2 id="dec-title">Decisions</h2></div>
            <div className="decisions-body">
              <ol id="plans" className="plans" />
              <article id="detail" className="detail"><p className="empty">Decisions appear as the run plays. Click one to see why.</p></article>
            </div>
          </section>

          <section className="side">
            <div className="panel-head"><h2>Event</h2></div>
            <div id="event" className="event"><p className="empty">Click a marker on the playback bar.</p></div>
            <div className="panel-head"><h2>Incidents</h2></div>
            <ol id="incidents" className="incidents" />
          </section>

          <section className="hero" aria-label="About">
            <div className="nemo">
              <span className="eyebrow">Where Nemotron comes in</span>
              <p>Hospital notes are messy: <q>CT 2, needs large car</q>. Nemotron reads them, and weighs the options when rules can&apos;t decide. A policy gate approves every move; the LLM never drives a robot.</p>
            </div>
            <p id="aha" className="aha" hidden />
            <div className="wwh">
              <div><span className="eyebrow">Problem</span><p>AMRs and patient beds share lifts. The AMR traffic system can&apos;t see patients, so urgent oxygen stalls, or a patient waits.</p></div>
              <div><span className="eyebrow">Approach</span><p>Read the hospital&apos;s transport requests, forecast lift use minutes ahead, rank by clinical priority.</p></div>
              <div><span className="eyebrow">Action</span><p>Close a lift to AMRs, reroute, hold or hand over the delivery, through Open-RMF, and log it.</p></div>
            </div>
          </section>
        </main>

        {/* ======================================================== playground */}
        <main className="try-main" id="try-view" hidden>
          <section className="try-form" aria-labelledby="try-title">
            <h1 id="try-title">Playground: write a messy note, watch Nemotron read it.</h1>
            <p>The patient transport from scenario 1, editable. Nemotron Nano on Nebius Token Factory parses it live; Auspex then decides against the recorded moment.</p>
            <p className="note">Input is treated as untrusted data, never as instructions. Nothing here moves a robot.</p>
            <div className="examples" role="group" aria-label="Examples" id="examples" />
            <form id="try-form" className="fields">
              <label>Destination, as received <input name="destination_text" required maxLength={200} autoComplete="off" /></label>
              <label className="wide">Notes, as received <textarea name="notes" rows={2} maxLength={200} /></label>
              <label>Mode <select name="transport_mode"><option>bed</option><option>wheelchair</option><option>stretcher</option><option>walking</option></select></label>
              <label>Bed type <select name="bed_type"><option>bariatric</option><option>standard</option><option>crib</option><option value="">none</option></select></label>
              <label>Priority <select name="priority"><option>Stat</option><option>High</option><option>Normal</option><option>Low</option></select></label>
              <label>Responding at <select name="status_time" id="status-time" /></label>
              <div className="wide actions-row"><button type="submit" className="approve" id="try-submit">Ask Nemotron</button><span id="try-status" className="note" aria-live="polite" /></div>
            </form>
          </section>
          <section className="try-result" aria-labelledby="try-result-title">
            <h2 id="try-result-title">Decision</h2>
            <p id="try-label" className="replay-banner inline" hidden />
            <div id="try-summary" className="summary" />
            <article id="try-detail" className="detail"><p className="empty">Submit, or pick an example.</p></article>
          </section>
        </main>

        {/* ======================================================== benchmark */}
        <main className="page-main" id="results-view" hidden>
          <section className="bench" aria-labelledby="bench-title">
            <h1 id="bench-title">Benchmark</h1>
            <p className="lead">A = AMRs alone (Open-RMF). B = Auspex, rules only. C = Auspex + Nemotron. Same scenario scripts, measured on deliberately hard cases.</p>
            <div id="bench" />
          </section>
        </main>

        {/* ======================================================== ledger */}
        <main className="page-main" id="ledger-view" hidden>
          <section className="ledger-panel" aria-labelledby="ledger-title">
            <div className="panel-head">
              <h1 id="ledger-title">Audit ledger</h1>
              <div className="actions-row">
                <button id="verify" className="approve">Verify chain</button>
                <button id="tamper" className="reject">Tamper with an entry</button>
                <button id="reset" className="link">Reset</button>
              </div>
            </div>
            <p className="lead">Append-only, SHA-256 hash-chained. Every fleet change is written here before it is reported done. Verification runs in your browser.</p>
            <p id="ledger-run" className="note" />
            <p id="ledger-status" className="note" />
            <ol id="ledger" className="ledger" />
          </section>
        </main>

        {/* ======================================================== self-host */}
        <main className="page-main" id="setup-view" hidden>
          <section className="doc" aria-labelledby="setup-title">
            <h1 id="setup-title">Self-host: run the full stack locally</h1>
            <p className="lead">Simulated clinic (Gazebo + Open-RMF), ROS bridge, Auspex core (FastAPI + Postgres), Nemotron on Nebius Token Factory. One machine.</p>
            <h2>Requirements</h2>
            <ul>
              <li>Windows with WSL2 running Ubuntu 24.04 (or native Ubuntu 24.04). 16 GB RAM recommended; with the Gazebo GUI, more.</li>
              <li>ROS 2 Kilted, <code>ros-kilted-rmf-dev</code> 0.2.0, <code>rmf_demos</code> built in <code>~/rmf_ws</code></li>
              <li>Docker Engine inside WSL2, <code>uv</code>, Python 3.12</li>
              <li>Node 20+ installed inside WSL (not the Windows one) for this site</li>
              <li>Optional: Nebius Token Factory and Tavily API keys. Without them a deterministic stand-in model runs.</li>
            </ul>
            <h2>Steps</h2>
            <p className="note">Each long-running command gets its own terminal. All commands are bash inside WSL.</p>
            <ol className="steps">
              <li><h3>Clone and bootstrap</h3>
                <pre><code>{`git clone ${REPO_URL}
cd auspex
cp .env.example .env        # set the passwords; API keys optional
scripts/bootstrap           # Python environments (core + ROS bridge)
scripts/check-env`}</code></pre></li>
              <li><h3>Database</h3>
                <pre><code>{`scripts/db-up
scripts/migrate`}</code></pre></li>
              <li><h3>Open-RMF API server</h3> (terminal 1; the fleet adapters connect to it)
                <pre><code>{`docker run --network host -it --rm ghcr.io/open-rmf/rmf-web/api-server:kilted-nightly`}</code></pre></li>
              <li><h3>Simulation</h3> (terminal 2). Headless by default: no Gazebo window, lowest memory.
                <pre><code>{`scripts/run-sim`}</code></pre>
                To watch the robots in Gazebo and RViz instead (needs WSLg or an X server, and noticeably more memory):
                <pre><code>{`scripts/run-sim --gui`}</code></pre></li>
              <li><h3>ROS bridge</h3> (terminal 3)
                <pre><code>{`scripts/run-bridge`}</code></pre></li>
              <li><h3>Core</h3> (terminal 4). Capture the nav graphs once with the sim up, then start the core.
                <pre><code>{`scripts/capture-navgraphs
scripts/run-core            # ops console at http://127.0.0.1:<CORE_PORT>/ui/`}</code></pre></li>
              <li><h3>Run a scenario</h3> in mode A, B or C (terminal 5)
                <pre><code>{`scripts/run-scenario s1 --config C
scripts/run-scenario s2 --config C
scripts/run-scenario s3 --config C`}</code></pre></li>
              <li><h3>Switch to Nemotron</h3> Set <code>LLM_PROVIDER=tokenfactory</code>, <code>NEBIUS_API_BASE</code>, <code>NEBIUS_API_KEY</code>, <code>MODEL_TRIAGE</code> and <code>MODEL_REASON</code> in <code>.env</code>, restart <code>run-core</code>, then check:
                <pre><code>{`scripts/check-llm`}</code></pre></li>
              <li><h3>Record replays and the benchmark</h3>
                <pre><code>{`scripts/record-replays
scripts/build-web-data`}</code></pre></li>
              <li><h3>This site</h3> (needs only the recordings, not the stack above)
                <pre><code>{`cd web && npm install && npm run dev    # http://127.0.0.1:8090/`}</code></pre></li>
              <li><h3>Playground backend</h3> (Python only, no ROS or database)
                <pre><code>{`scripts/run-sandbox --check
scripts/run-sandbox                    # http://127.0.0.1:8095
echo "SANDBOX_URL=http://127.0.0.1:8095" > web/.env.local`}</code></pre></li>
            </ol>
          </section>
        </main>

        {/* ======================================================== rate limits */}
        <main className="page-main" id="usage-view" hidden>
          <section className="doc" aria-labelledby="usage-title">
            <h1 id="usage-title">Usage &amp; rate limits</h1>
            <h2>This site</h2>
            <ul>
              <li><strong>Simulation:</strong> pick a scenario and mode, press Play. Markers on the bar jump to key events. &quot;Side by side&quot; compares two modes.</li>
              <li><strong>Playground:</strong> edit the transport request and call Nemotron. If the backend is off or the budget is spent, you get the recorded answer.</li>
              <li><strong>Benchmark:</strong> measured A/B/C results.</li>
              <li><strong>Audit ledger:</strong> verify the hash chain of the run you&apos;re watching, or tamper with it.</li>
            </ul>
            <h2>LLM tokens per recorded run</h2>
            <div id="usage-tokens" />
            <h2>Playground limits</h2>
            <ul id="usage-limits">
              <li>Total LLM budget: <strong id="limit-budget">$0.02</strong>. When it&apos;s spent, the recorded answer is shown.</li>
              <li>Per-IP rate limit and a daily call cap.</li>
              <li>Identical inputs are served from cache, at no cost.</li>
              <li>200 characters per field; strict request schema.</li>
            </ul>
            <p id="usage-live" className="note" />
          </section>
        </main>

      </div>
    </div>
    </>
  );
}
