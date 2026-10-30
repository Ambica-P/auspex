# Auspex benchmark: results on the edge-case scenario set

Scenarios S1-S3 and variants are deliberately weighted to edge cases where the right
action cannot be computed from structured fields alone (spec 8.1). These are results
on that set, not general performance. A: Open-RMF alone (Auspex observes). B: Auspex
deterministic (playbooks, evaluator). C: Auspex with Nemotron; the model is named.
Generated 2026-10-29T22:27 from 12 runs.

| Scenario | Config | Runs | All checks pass | O2 on time | Deadline margin s (median) | Max porter delay s (median) | Max robot lift wait s (median) | Conflict detected | Plans applied (median) | Decision latency ms (median, by tier) | LLM tokens per run (median) |
|---|---|---|---|---|---|---|---|---|---|---|---|
| s1 | A | 1 | 1/1 | 1/1 | 226 | 6 | 133 | 0/1 | 0 | n/a | n/a |
| s1 | B | 1 | 1/1 | 1/1 | 224 | 6 | 124 | 0/1 | 1 | fast_lane 59 | n/a |
| s1 | C (nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B) | 1 | 1/1 | 1/1 | 265 | 14 | 0 | 1/1 | 2 | fast_lane 46 | 1076 |
| s2 | A | 1 | 1/1 | 1/1 | 282 | 13 | 91 | 0/1 | 0 | n/a | n/a |
| s2 | B | 1 | 1/1 | 1/1 | 226 | 13 | 0 | 0/1 | 3 | fast_lane 67 | n/a |
| s2 | C (nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B, nvidia/nemotron-3-super-120b-a12b) | 1 | 1/1 | 1/1 | 274 | 13 | 82 | 0/1 | 4 | agent 21452, fast_lane 41 | 4264 |
| s2_e42 | A | 1 | 1/1 | 0/1 | n/a | 13 | 578 | 0/1 | 0 | n/a | n/a |
| s2_e42 | B | 1 | 1/1 | 0/1 | n/a | 8 | 0 | 0/1 | 2 | fast_lane 73 | n/a |
| s2_e42 | C (nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B, nvidia/nemotron-3-super-120b-a12b) | 1 | 1/1 | 1/1 | 386 | 16 | 0 | 0/1 | 2 | agent 16364, fast_lane 37 | 3914 |
| s3 | A | 1 | 1/1 | 0/1 | n/a | 159 | 428 | 1/1 | 0 | n/a | n/a |
| s3 | B | 1 | 1/1 | 0/1 | n/a | 153 | 0 | 1/1 | 3 | fast_lane 38 | n/a |
| s3 | C (nvidia/nemotron-3-super-120b-a12b) | 1 | 1/1 | 1/1 | 175 | 157 | 0 | 1/1 | 4 | agent 6079, fast_lane 36 | 3273 |

Deadline margin: seconds the oxygen arrived before its deadline (negative: late; n/a: not delivered in the run). Porter delay: request to lift granted. Decision latency: trigger to plan submitted (fast_lane budget 2000 ms; agent within AGENT_BUDGET_S). Checks: each scenario's ground truth for that configuration.
