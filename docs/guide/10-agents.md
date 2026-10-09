# Phase 3: Agents

Goal: agents that look things up on their own (investigate a finding, triage new log patterns, suggest tuning, answer questions) while staying read-only, bounded, auditable, and fully optional. Phase 3 starts after the AI provider layer (Task 24), the network map data (Tasks 19-20), and the MCP server (Task 26) exist, because agents use the same tools.

## What the agents do

| Agent | Trigger | Output |
| --- | --- | --- |
| Investigator | A finding at or above a rank threshold, or "Investigate" in the GUI | A short note attached to the finding: summary, cited evidence, likely explanations with confidence, read-only next checks |
| Triage | New signatures after the learning period | A verdict per signature (noise, watch, concern, unsure) and optional proposals (ignore rule, playbook rule) |
| Tuner | Weekly | Up to three proposals (thresholds, routing) each tested with a replay: pushes before and after, findings that would be missed |
| Ask the lab | Chat box in the admin GUI | Short answers with citations |

Agents are defined in `brief/agents.yaml` (trigger, allowed tools, provider chain, limits, output, prompt) and their prompts live in `brief/prompts/`.

## How they work

- **One tool registry, two front doors.** The read-only tools (status, findings, a single finding, history, signature details, log search, inventory, Unraid notifications, systems, map, replay) are defined once. The MCP server exposes them to outside AI clients; the internal agent loop offers them to models. Neither can do more than the other.
- **Standard tool calling.** The loop uses the OpenAI-compatible tool-calling format, so any model that supports tools works (verified by "Test connection").
- **A small loop of our own** (a few hundred lines), not a heavy agent framework: easy to audit, cheap to run, and predictable. Outside frameworks (Home Assistant, Claude, n8n, LangGraph, others) can still drive the product through MCP.
- **Background only.** Agents run from a queue after the brief is published, never in its critical path. With `run_agents_only_when_gpu_idle`, they wait while the model server is busy and skip if it stays unavailable.

## Guardrails

Locked (always on, shown on the AI page):

- **Read-only tools.** No agent tool can change config, touch a system, or send a notification.
- **Proposals, not changes.** An agent's suggested change becomes a proposal with a diff. You approve or reject it on the AI page; approval goes through the normal config path (validation, versioning, rollback, audit log).
- **Evidence or it is dropped.** Every factual statement must cite a tool result id. Uncited statements are removed before anything is shown.
- **Hard limits per run:** steps, tool calls, tokens, and time (defaults in `agents.yaml`).
- **Privacy:** tool results containing device-level detail, DNS data, or raw lines never go to a provider marked `cloud`. If an agent's chain has only cloud providers, those tools are withheld from it.
- **Safety lines in the prompts:** triage never proposes ignoring disk, authentication, or security events; the tuner never proposes disabling disk, security, or absence alerts, or anything that would have hidden an acknowledged or urgent finding.
- **Everything logged:** each run's trigger, steps, tools called, tokens, outcome, and the decision on each proposal.

Adjustable (per agent and globally): on/off, triggers and thresholds, allowed tools (narrowing only), provider chain, step and token limits, daily run caps, GPU-idle and quiet-hours rules.

## Small local models

An 8B model on an 8 GB card handles short, structured tool loops well and long open-ended investigations poorly. So:

- Keep each agent's tool list short and its output structured (JSON or a fixed note format).
- Keep the step budget small (8 by default).
- For "Ask the lab", optionally allow a stronger cloud model in `allow_cloud_for`; the privacy rule still withholds device-level tools from it.

## Evaluation

- **Agent fixtures:** past cases with known good outcomes, shipped in `brief/fixtures/agents/` (the slow parity check, the hot access point, the collection outage, a noisy new signature, a plain question, a quiet tuning week) and added to by your own files in `/config/brief/fixtures/agents/`. Each agent is replayed against its cases in a scratch copy after any change of model, prompt, tools, limits, or chain (the config editor does it before saving).
- **Metrics on the AI page:** citation rate, dropped-claim rate, proposals accepted versus rejected, average steps and latency per run.
- A model or prompt change that lowers the fixture score is flagged before it is enabled.

## Done when

- With `ai.yaml` `enabled: false`, no agent runs, no model is called, and the MCP server is off.
- The investigator attaches a cited note to a fixture finding; any uncited claim in its output is removed.
- The triage agent's ignore proposal appears on the AI page with a diff, does nothing until approved, and after approval is applied through the normal config path with an audit entry.
- The tuner's proposal includes replay evidence and is rejected automatically if it would have hidden an acknowledged or urgent finding.
- With only a cloud provider in an agent's chain, device-level tools are withheld from that agent.
