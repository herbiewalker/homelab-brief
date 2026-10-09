# Running a local model for the AI features (Ollama on a GPU server)

The AI features are optional and off by default. If you want them without sending anything to a hosted service, run a model server on a machine with a GPU, as its own container separate from Homelab Brief, so it can also serve Home Assistant's Assist. Any OpenAI-compatible server works (Ollama, llama.cpp server, vLLM, LM Studio, a LiteLLM gateway); this page uses Ollama because it is the easiest.

Nothing here is required. With AI off, or with the model server down, every task falls back to the plain template.

## 1. The container

Install the NVIDIA driver plugin on the GPU server first (on Unraid, from the Community Applications plugin list), then run Ollama with the NVIDIA runtime. A Compose Manager stack:

```yaml
services:
  ollama:
    image: ollama/ollama:<pin>              # an exact version tag, never latest
    container_name: ollama
    restart: unless-stopped
    runtime: nvidia
    environment:
      NVIDIA_VISIBLE_DEVICES: all
      NVIDIA_DRIVER_CAPABILITIES: compute,utility
      OLLAMA_CONTEXT_LENGTH: "8192"         # cap the context so the KV cache stays inside the card's memory
      OLLAMA_FLASH_ATTENTION: "1"
      OLLAMA_KV_CACHE_TYPE: q8_0            # shrinks the context's memory so the whole model stays on the GPU
      OLLAMA_KEEP_ALIVE: 5m                 # unload from VRAM after five idle minutes (a loaded card draws power)
      OLLAMA_MAX_LOADED_MODELS: "1"         # two 8B models will not fit together in 8 GB; share one copy
    volumes:
      - /mnt/user/appdata/ollama:/root/.ollama
    ports:
      - "<model-server-ip>:11434:11434"     # bind to the LAN address, not 0.0.0.0
```

Variable names change between Ollama releases; check them against the version you installed. If the context overflows the card's memory, Ollama quietly moves layers to the CPU and gets much slower, so keep the context cap and `context_tokens` in `ai.yaml` the same number. After the first answer, `docker logs ollama | grep -iE "offload|layers|memory"` should show every layer on the GPU.

## 2. Choose and pull a model

A small instruction-tuned model is enough: the tasks are a digest of at most about 150 lines in and a few hundred words out.

| Card memory | A reasonable start | Notes |
|---|---|---|
| 8 GB | a 9B model at 4-bit, or a 3.8B one | The 3.8B leaves room for other GPU work |
| 12 GB or more | an 8B to 14B model | More room for context |

Pull it (`docker exec ollama ollama pull <model>`), then compare it with the template on the AI page's side-by-side preview with real digests before relying on it. Record the model and its licence in your own notes. If the model has a separate "thinking" mode, leave `disable_thinking: true` in `ai.yaml`; the product also strips any reasoning text before checking the answer.

## 3. Control who can reach it

Ollama has no authentication. Do one of these, and never expose the port beyond your LAN:

- a firewall rule that allows only the Homelab Brief container's address (and Home Assistant) to reach port 11434 on the GPU server; or
- a small gateway with an API key in front of it (LiteLLM or a reverse proxy). Then put the key in a Homelab Brief template variable and name it in `api_key_var`; the AI page shows only that a key is set, never its value.

## 4. Point Homelab Brief at it

On the AI page (or in `ai.yaml`), for the provider called `local`:

1. Set `base_url` to `http://<model-server-ip>:11434/v1` in the config editor (the page edits switches and models, not addresses).
2. **List models** and choose one, then switch the provider on.
3. **Test connection**: it makes one tiny real call and reports latency, whether your chosen model is among those listed, and whether the model supports tool calls (agents, a later feature, need that).
4. Turn on the master switch and the features you want, one at a time (morning brief, explain finding, weekly summary). Each feature has its own fallback chain that ends in `template`.

From a shell, the same steps are `brief ai status`, `brief ai models local`, `brief ai provider local model NAME`, `brief ai provider local on`, `brief ai test local`, `brief ai enable`, and `brief ai feature morning_brief on`.

## 5. Check it behaves

- `brief ai preview` (or **Compare** on the page) shows today's template brief next to the model's version. A reply that names a host, address, device, or finding that was not in its input is rejected and the template stands; the call log says why.
- The call log records provider, model, task, time, tokens, and outcome, never the content. Tests, previews, and explanations are real calls and count toward the daily limits.
- A cloud provider (`locality: cloud`) is a different matter: it must be allowed per feature (`allow_cloud_for`), receives only system-level facts, and is blocked before sending if the text holds an address or a private name. Findings about one household device are never sent to it.

## Home Assistant

Point Home Assistant's Ollama (or OpenAI-compatible) integration at the same server so one loaded model serves both. Integration names and options change between Home Assistant releases; verify against your installed version.
