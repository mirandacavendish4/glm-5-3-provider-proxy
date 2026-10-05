# GLM-5.3 — Provider Proxy And Compatibility Layer

Argolink-focused provider proxy and compatibility layer for GLM-5.3. This repository is a self-contained integration reference: it explains the request shape, the asynchronous lifecycle where applicable, safe retries, and the failure states that matter in production.

[GLM-5.3 model page →](https://argolink.io/gh-glm-5-3-provider-proxy) · [Argolink API docs →](https://argolink.io/gh-unified-api-docs)

The model page is authoritative for live availability, pricing, limits, and fields. This repository gives the workflow a searchable home; it does not invent a second model catalog.

## What is inside

- `docs/model-contract.md`: model-specific facts checked against the current Argolink catalog.
- `docs/workflow.md`: the intent behind this repository and the decision points that prevent accidental resubmission.
- `examples/request.sh` and `examples/client.py`: safe, runnable request examples using `ARGOLINK_API_KEY`.
- `tests/test_repository.py`: offline checks for the public contract and examples.
- `ARGOLINK.md`: public-boundary rules for contributors.

## Quick start

Set the key only in your shell or secret manager, then run the example:

```bash
export ARGOLINK_API_KEY='your-key'
./examples/request.sh
```

The example uses model ID `glm-5.3` and endpoint family `/v1/chat/completions`. The response is JSON; keep the returned request ID and redacted error body when diagnosing a failure. A timeout does not prove that a request was rejected, so do not submit a second request automatically.

## Model facts

- Model ID: `glm-5.3`
- Argolink model page: `https://argolink.io/gh-glm-5-3-provider-proxy` (tracked shortlink; destination is the model page)
- Endpoint family: `/v1/chat/completions`
- Context: `1,000,000` tokens; max output: `128000` tokens.
- Reasoning efforts: `low, high, max`; default: `max`.
- Listed token rates: input `$1.4` / output `$4.4` per million tokens; cached input `$0.26` when listed.

## Scope

This repository has one clear search intent: **provider proxy and compatibility layer**. It keeps the integration boundary small enough for a developer or an AI assistant to answer a concrete question, while linking the live Argolink model page for changes.

## Safety

Never commit an API key, a returned media URL containing a private token, or production input. Examples are deliberately small and do not make a request during tests.
