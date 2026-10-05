# Chat request boundary

`glm-5.3` uses `/v1/chat/completions`. Keep the request small enough to debug and record the returned request ID.

- Authentication: `Authorization: Bearer $ARGOLINK_API_KEY`
- Model: `glm-5.3`
- Base URL: `https://api.argolink.io`
- Reasoning choices currently listed for this model: `low, high, max`.

Do not silently translate a timeout into a second submission. When a tool call or structured response is required, keep the response envelope and error body in the redacted diagnostic record.
