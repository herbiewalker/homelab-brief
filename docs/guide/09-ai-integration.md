# AI integration (any model, any host)

Goal: the product works with any LLM, local or hosted, through industry-standard interfaces, with the GPU server's GPU as the expected local option. Home Assistant's own AI can use the same models and can see this product's data. Nothing depends on one vendor.

## Principles

1. **Rules decide; AI explains.** Detectors, the playbook, and routing decide findings, severity, and alerts. AI only rewrites or explains findings that already exist, and never takes actions.
2. **The pipeline never waits on AI.** Every AI task has a fallback chain that ends in the template (no AI). Any error, timeout, budget limit, or failed validation moves to the next step.
3. **Standards only.** Models are reached through the OpenAI-compatible Chat Completions API (with the native Anthropic API as one optional adapter). Tools and context are offered to AI clients through MCP. Prompts are plain Markdown files.
4. **Privacy by locality.** Each provider is marked `lan` or `cloud`. Cloud providers never receive per-device detail, raw log lines, DNS data, or anything from `devices.yaml`.
5. **Light on resources.** A few calls a day on a small digest; models load only when needed.

## Standards used

| Direction | Standard | Used for |
| --- | --- | --- |
| Container calls a model | OpenAI-compatible Chat Completions (`/v1/chat/completions`, `/v1/models`) | Ollama, llama.cpp server, vLLM, LM Studio, LocalAI, LiteLLM gateway, hosted services |
| Container calls a model (optional) | Native Anthropic Messages API | Only if you prefer it over an OpenAI-compatible route |
| AI clients call the container | MCP (Model Context Protocol), Streamable HTTP | Home Assistant's MCP client, Claude Desktop or Claude Code, other MCP-capable assistants |
| Home Assistant's AI sees the data | Home Assistant entities (via MQTT discovery) | Assist and conversation agents can answer from the exposed sensors |
| Prompts and context | Plain Markdown (`brief/prompts/*.md`, the digest) | Readable by any model; no vendor-specific syntax |

## Provider layer

Configured in `brief/ai.yaml` and on the admin GUI's AI page.

- **Providers:** any number, each with `type`, `base_url`, `model`, API key variable, `locality`, timeout, output limit, and context size.
- **Tasks:** `morning_brief`, `explain_finding`, and `weekly_summary`, each with its own chain (for example `[local, cloud, template]` or `[local, template]`) and prompt file. New tasks are added in config, not code.
- **Context fitting:** the input (the digest, at most about 150 lines) is trimmed to the provider's `context_tokens`, dropping the lowest-ranked findings first.
- **Validation:** every host, device, and finding id mentioned in the output must exist in the input. Output that fails falls through to the next provider.
- **Budgets:** a daily cap on calls and on cloud tokens.
- **Call log:** provider, model, task, latency, token counts, and outcome (not the content), shown on the AI page.
- **GUI tools:** "Test connection", "List models" (from `/v1/models`), a latency test, and a side-by-side preview of the template brief and each provider's version of today's brief.

## AI admin page

One page controls all AI, laid out top to bottom:

1. **Master switch** (`enabled` in `ai.yaml`). Off means no model calls, agents stopped, and the MCP server closed; the product keeps working on templates and rules. The page header always shows the current state.
2. **Feature switches:** morning brief rewrite, explain finding, weekly summary, agents, ask the lab, MCP server. Each shows its provider chain and today's usage.
3. **Providers:** each with on/off, type, address, model, locality badge (`lan` or `cloud`), "Test connection" (also checks tool-calling support), "List models", and latency.
4. **Guardrails**, in two clearly separated groups:
   - *Locked (always on):* listed with a one-line explanation each (never waits on AI, never decides or acts, cloud privacy, read-only tools, proposals need approval, citations required, everything logged). They have no switch, so they cannot be turned off by accident.
   - *Adjustable:* daily call and cloud-token budgets, which features may use cloud providers at all, output validation mode, agent limits (steps, tool calls, tokens, runs per day), GPU-idle and quiet-hours rules. Each shows its current value, a short description, and the change history.
5. **Agents:** each agent's switch, trigger, allowed tools, limits, last runs, and fixture score.
6. **Proposals:** pending agent proposals with diffs and replay evidence; approve or reject (both logged).
7. **MCP clients:** tokens with scopes; create, revoke, last used.
8. **Prompts:** editor with history and rollback.
9. **Preview and log:** side-by-side template versus provider output for today's brief, and the call log (no content).

Every change on this page goes through the same validation, versioning, rollback, and audit log as any other config.

## The local GPU server

Run the model server as its own container on the GPU server, separate from this product, so it can serve both this container and Home Assistant.

| Server | Strengths | Trade-offs |
| --- | --- | --- |
| Ollama | Easiest; model management built in; OpenAI-compatible `/v1`; HA has a native integration | Less tuning control |
| llama.cpp server | Leanest; OpenAI-compatible | Manual model files and settings |
| vLLM | Highest throughput | Heavier; needs more VRAM; overkill for a few calls a day |

Recommended: Ollama on the GPU server with the NVIDIA runtime (on Unraid, install the NVIDIA driver plugin first).

- **Access control:** Ollama has no built-in authentication. Either allow only this container's IP and the Home Assistant to reach the GPU server's model port with a UniFi firewall rule, or put a small gateway with an API key (LiteLLM or a reverse proxy) in front. Never expose it beyond the LAN.
- **Power:** set a short keep-alive so the model unloads from VRAM after use, and check the GPU server's idle draw with the GPU loaded and unloaded.
- **Availability:** the container health-checks the provider before each task. If the GPU server is down, the chain falls back and the brief still goes out.
- **Model choice:** start with a small instruction-tuned model that fits the GPU server's VRAM, compare it with the template on the AI page's preview, and record the choice and its license in `docs/DECISIONS.md`.

### Sizing for an 8 GB GPU (an RTX 3060 Ti as the example)

The 8 GB has to hold the quantized weights, the context (KV cache), and runtime overhead. If the context overflows VRAM, Ollama quietly moves layers to the CPU and gets much slower, so cap the context.

| Role | Model (Ollama tag, verify current) | Size at Q4_K_M | License | Why |
| --- | --- | --- | --- | --- |
| Primary | Qwen3.5 9B (`qwen3.5:9b`) | about 6.6 GB | Apache-2.0 | Current default pick for 8 GB cards; strong instruction following; small KV cache |
| Lighter | Phi-4-mini 3.8B (`phi4-mini`) | about 2.5 GB | MIT | Leaves room for other GPU work on the GPU server or a second loaded model |
| Fallback | Llama 3.1 8B (`llama3.1:8b`) | about 4.7 GB | Llama community license (not OSI open source) | Widely compatible if a newer model misbehaves |

This product's tasks are small (a digest of at most about 150 lines in, a few hundred words out), so an 8B-class model at Q4 answers in seconds on this card.

Ollama settings on the GPU server (environment variables on the Ollama container; verify names against the installed version):

- `OLLAMA_CONTEXT_LENGTH=8192`: enough for the digest plus prompt and output, and safely inside VRAM. Match `context_tokens` in `ai.yaml`.
- `OLLAMA_FLASH_ATTENTION=1` and `OLLAMA_KV_CACHE_TYPE=q8_0`: shrink the context's memory so the whole model stays on the GPU. Set them together with the context length.
- `OLLAMA_KEEP_ALIVE=5m`: unload from VRAM after five idle minutes to save power (a loaded but idle 3060 Ti was measured at about 73 W in P2).
- `OLLAMA_MAX_LOADED_MODELS=1`: two 8B models will not fit together in 8 GB. Use the same model for this product and Home Assistant's Assist so they share one loaded copy instead of swapping.

**Measured on an RTX 3060 Ti:** before the variables, `qwen3.5:9b` loaded as 6.0 GB with a 35% CPU / 65% GPU split at 4096 context. After setting `OLLAMA_CONTEXT_LENGTH=8192`, `OLLAMA_FLASH_ATTENTION=1`, and `OLLAMA_KV_CACHE_TYPE=q8_0`: `qwen3.5:9b` 5.5 GB at 100% GPU and `phi4-mini` 3.2 GB at 100% GPU, both with 8192 context. With `--think=false`, Qwen3.5 answered directly. If a future model shows a split, read the load log (`docker logs ollama | grep -iE "offload|layers|memory"`) and consider a smaller quantization or a text-only model.

**Thinking mode:** Qwen3.5 thinks by default. The provider layer must turn it off for these tasks (Ollama's native API has a `think` option; check how the installed version exposes it on the OpenAI-compatible endpoint) and strip any reasoning text that still appears. On the command line, test with `ollama run <model> --think=false`.

If the model has a separate "thinking" mode, turn it off for these tasks (faster and more predictable), and have the provider layer strip any reasoning block before validation. Do the quality check on the AI page's side-by-side preview with real digests before trusting it, and leave the template as the last step of every chain.

## Home Assistant's AI

Home Assistant has its own AI features (Assist with conversation agents, and AI integrations for Ollama and OpenAI-compatible or hosted models). Integration names and options change between HA releases, so verify against your installed version.

- **Same model, both sides:** point HA's Ollama (or OpenAI-compatible) integration at the same server on the GPU server. One GPU serves both.
- **HA's AI can see this product's data:** every sensor this container publishes is an HA entity. Expose the ones you want to Assist, and HA's conversation agent can answer "is anything wrong in the homelab?" from them.
- **Deeper questions through MCP:** this container runs an MCP server (below). HA's MCP client integration can add its tools to Assist, so HA's AI can list findings, explain one, or pull history.
- **Independence:** neither side depends on the other's AI. If HA's AI is off, this product still works, and the reverse.

## MCP server (container to any AI client)

- **Transport:** Streamable HTTP at `https://<advertised-address>/mcp`, plus `docker exec -it homelab-brief brief mcp` (stdio) for local clients.
- **Authentication:** bearer token per client (created and revoked on the AI page, stored hashed). Each token has a scope.
- **Tools (all read-only):** `get_status`, `list_findings`, `explain_finding`, `get_history` (aggregated series for a signature or metric), `list_systems`, `get_map`, `search_logs` (bounded results; only for tokens scoped to it).
- **Resources:** the latest digest, the latest weekly report, the systems list.
- **Privacy:** the default scope excludes per-device activity, DNS data, and raw log lines; those need an explicitly scoped token, and never go to a client you have marked as cloud.
- **Audit:** every MCP call is recorded (client, tool, time, outcome).

## Context notes for any agent

- **Coding agents:** project instructions are in `AGENTS.md` (the vendor-neutral convention). `CLAUDE.md` only points to it. The docs under `docs/` are written for any agent or person.
- **Runtime models:** prompts live in `brief/prompts/` as plain Markdown and are editable in the GUI with history and rollback, like any config.
- **The digest is the universal context pack:** a small, ranked Markdown summary that any model, MCP client, or person can read.

## Done when

- The same task runs against at least two different OpenAI-compatible servers (for example Ollama locally and a LiteLLM gateway) by changing only `ai.yaml`.
- With every provider down, the brief publishes from the template, and the AI page shows why.
- Output naming an unknown host fails validation and falls through.
- A cloud provider never receives device-level detail (tested with a fixture containing it).
- An MCP client with a default-scope token can list findings and history but cannot reach device detail or raw logs; every call appears in the audit log.
- Home Assistant, pointed at the same model server, can answer a question about the homelab from the exposed sensors.
