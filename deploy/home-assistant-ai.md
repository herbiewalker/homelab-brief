# Home Assistant and the AI features

Two independent links. Neither needs the other, and the product runs without both.

## 1. Sensors in Assist (no MCP, works today)

Every sensor this product publishes over MQTT discovery (system states, pipeline numbers, power) is a Home Assistant entity.

1. Settings, Voice assistants, Expose: tick the entities you want Assist to see (the per-system state sensors and the headline are the useful ones).
2. Point HA's conversation agent at the same model server this product uses (for example Ollama: the HA **Ollama** integration with the server's address and a model), or any OpenAI-compatible server through HA's OpenAI integration with a custom base URL. Check the exact integration names in your HA version: they have been renamed before.
3. Ask: "is anything wrong in the homelab?" The agent answers from the exposed states.

## 2. This product's MCP tools in HA (needs a bridge)

The product serves MCP over **Streamable HTTP** at `https://<address>/mcp`, with a **bearer token** (make one on the AI page, or `brief mcp token-create home-assistant`). Turn on the master switch and the `mcp_server` feature first; with them off the endpoint answers 404.

As checked against the Home Assistant documentation on 2026-10-09, HA's **Model Context Protocol** integration (added in 2025.2) is a client that speaks only the **SSE** transport, authenticates only with **OAuth** (client id and secret), and uses tools only (no resources or prompts). So it cannot connect to `/mcp` directly. Options:

- **A bridge that speaks SSE to HA and Streamable HTTP with a bearer header to this product** (for example the open-source `mcp-proxy`, run in a small container next to HA). The bridge's flags and whether HA accepts it without OAuth are NOT verified here: test it, and keep the token in the bridge's environment, never in a config you commit.
- **Do without it:** most of what Assist needs is already in section 1. Use MCP from clients that support it directly: Claude Desktop or Claude Code (Streamable HTTP with a header), or any MCP client.

Tokens for HA should have the default scope only (status, findings, history, systems, map). Leave `logs`, `devices`, and `dns` off.

## Checking a token from a shell

```bash
curl -sk https://<address>/mcp -H "Authorization: Bearer <token>" -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'
```

A `404` means the switches are off; a `401` means the token is wrong or revoked; `429` means too many failed tries (wait five minutes).
