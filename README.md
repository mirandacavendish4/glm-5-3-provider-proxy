# Claude Code to GLM 5.3 Messages proxy

This localhost-only proxy lets a client that is fixed to Anthropic's Messages
shape use GLM 5.3 through a compatible provider. It maps a requested Claude
model name to a real GLM model, forwards the request to `/v1/messages`, keeps
streaming responses intact, and records token usage in a local JSONL file. It
is useful when a tool refuses to change its model field; if the client already
supports `ANTHROPIC_BASE_URL` and `ANTHROPIC_MODEL`, direct configuration is
simpler. The default provider endpoint is [ArgoLink](https://argolink.io/gh-glm-5-3-provider-proxy),
with Z.ai as an optional fallback.

Last checked: 2026-10-07.

## Direct configuration first

Try this before running a proxy:

```bash
export ANTHROPIC_BASE_URL=https://argolink.io
export ANTHROPIC_AUTH_TOKEN="$GLM_API_KEY"
export ANTHROPIC_MODEL=glm-5.3
```

The Anthropic client appends `/v1/messages` to the root origin. Do not set the
base to `/v1`, or a client that appends the path will produce a duplicated
route. The proxy is for applications that insist on sending a Claude model
name or need provider fallback and a usage log.

## Run

```bash
python glm_proxy.py env
export GLM_API_KEY='...'
export GLM_PROXY_TOKEN='local-only-token'
cp proxy.example.json proxy.json
python glm_proxy.py serve --config proxy.json
```

Point Claude Code at the printed localhost URL and token. The server refuses a
non-loopback listen address because it holds upstream keys. `proxy.json` is an
local copy of the tracked `proxy.example.json`; keep any edited keys and
provider credentials outside the repository.
The `/health` endpoint reports configured provider names without revealing keys.

## Model mapping and price snapshot

| Requested client name | Forwarded model | ArgoLink input / cached / output per 1M | Z.ai input / cached / output |
|---|---|---:|---:|
| `claude-haiku-*` | `glm-5.3-flash` | $0.12 / $0.024 / $0.40 | $0.15 / $0.03 / $0.50 |
| other `claude-*` | `glm-5.3` | $1.12 / $0.208 / $3.52 | $1.40 / $0.26 / $4.40 |
| explicit `glm-5.2` | unchanged | $1.12 / $0.208 / $3.52 | $1.40 / $0.26 / $4.40 |

Prices were checked on the model catalog and Z.ai pricing page on 2026-10-07;
the proxy does not claim that two vendors have identical output behavior.
GLM 5.3 has a one-million-token context and 128K maximum output. Reasoning is
always on; use the documented `low`, `high`, or `max` effort values.

## Fallback behavior

Providers are tried in order for network failures and statuses that indicate
capacity or authentication trouble. A request validation error is returned to
the client instead of being replayed at another provider. Streaming is relayed
with chunked transfer encoding, and usage is collected from the final response
or message events. A failed fallback attempt is written as a short diagnostic,
never with the bearer key or request body.

The usage report groups calls by day, provider, and forwarded model. It is an
estimate based on the provider's published input, cache-read, and output rates;
it is not an invoice. Disable `usage_log` if the environment has a separate
observability policy.

## Request and response boundaries

The listener accepts only `POST /v1/messages` and `GET /health`. It keeps the
Anthropic request shape, changes only the model field and an absent effort
default, and forwards `anthropic-version` plus an opt-in beta header. JSON
responses are returned with their original status and body. Server-sent events
are passed through with HTTP chunk framing, so Claude Code can consume text as
it arrives. The proxy does not translate a Responses request into Messages and
does not expose a public listening address. A local token can protect the
listener even though it is bound to loopback.

Fallback is deliberately limited to network errors and provider statuses that
usually mean capacity or authentication trouble. A 400-level schema error is
returned immediately because replaying it at another provider cannot fix the
request. When all configured providers fail, the client receives a structured
529 error with provider names and short reasons; keys and request bodies are
never placed in that diagnostic. The JSONL usage record is written after the
upstream response and contains token counts, model, provider, effort and an
estimate from the configured rate table.

The included `proxy.example.json` is safe to commit because it contains no
credentials. Copy it to a private path, set the key environment variables, and
review provider order before running Claude Code. Keep the usage log outside a
public repository if prompts or model names are considered sensitive.

## Operational boundaries

- The proxy is a local adapter, not a public gateway.
- Keys belong only in environment variables or a secret manager.
- Keep `GLM_PROXY_TOKEN` enabled when Claude Code is not the only local client.
- A provider timeout does not prove that the upstream job was not accepted for
  a non-idempotent request; use a client-side request ID or do not replay.
- Test with a mock HTTP server before changing provider order.

## Tests

The tests cover wildcard model mapping, `[1m]` suffix handling, usage pricing,
loopback binding, and the Anthropic-shaped error envelope without contacting a
provider:

```bash
python -m pytest -q
python -m py_compile glm_proxy.py
```
