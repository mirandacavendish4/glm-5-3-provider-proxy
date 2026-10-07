"""A local Anthropic Messages proxy that runs Claude Code on GLM-5.3.

Claude Code -> http://127.0.0.1:8787/v1/messages -> an Anthropic-compatible GLM provider

It passes requests through unchanged except for four things:
  * the model name: Claude names (claude-opus-*, claude-haiku-*, ...) map to GLM IDs
  * the provider: the first provider that answers wins; 429, 5xx, 401/403 and
    network errors move to the next one before any byte reaches Claude Code
  * the effort: a default output_config.effort per model when the request has none
  * the bill: every response's token usage is appended to a JSONL log with its cost

    python glm_proxy.py serve --config proxy.json
    python glm_proxy.py env                      # Claude Code settings for the proxy
    python glm_proxy.py report usage.jsonl       # tokens and cost per day and model

Standard library only (Python 3.9+).
"""
from __future__ import annotations

import argparse
import datetime as dt
import fnmatch
import http.client
import json
import os
import socket
import sys
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

USER_AGENT = "glm-5-3-provider-proxy/0.1"  # Python-urllib/x.y is refused by some CDNs
FALLBACK_STATUSES = {401, 403, 408, 429, 500, 502, 503, 504, 529}

DEFAULT_CONFIG = {
    "listen": "127.0.0.1:8787",
    "proxy_token_env": "GLM_PROXY_TOKEN",
    "models": {
        "claude-haiku*": "glm-5.3-flash",
        "claude-*": "glm-5.3",
    },
    "effort": {"glm-5.3": "max"},
    "providers": [
        {"name": "argolink", "base_url": "https://argolink.io", "key_env": "GLM_API_KEY",
         "prices": {"glm-5.3": [1.12, 0.208, 3.52], "glm-5.3-flash": [0.12, 0.024, 0.40],
                    "glm-5.3-flashx": [0.296, 0.06, 1.0], "glm-5.2": [1.12, 0.208, 3.52]}},
        {"name": "zai", "base_url": "https://api.z.ai/api/anthropic", "key_env": "ZAI_API_KEY",
         "prices": {"glm-5.3": [1.4, 0.26, 4.4], "glm-5.3-flash": [0.15, 0.03, 0.5],
                    "glm-5.3-flashx": [0.37, 0.075, 1.25], "glm-5.2": [1.4, 0.26, 4.4]}},
    ],
    "forward_beta_headers": False,
    "usage_log": "usage.jsonl",
}


def load_config(path: str | None) -> dict:
    config = json.loads(json.dumps(DEFAULT_CONFIG))
    if path:
        with open(path, encoding="utf-8") as handle:
            config.update(json.load(handle))
    return config


def map_model(name: str, models: dict) -> str:
    """Strip Claude Code's [1m] marker, then use the first matching pattern (fnmatch, in order)."""
    bare = name.split("[", 1)[0]
    for pattern, target in models.items():
        if fnmatch.fnmatchcase(bare, pattern):
            return target
    return bare


def cost(prices: dict, model: str, usage: dict) -> float | None:
    rates = prices.get(model)
    if not rates:
        return None
    price_in, price_cached, price_out = rates
    total = (usage.get("input_tokens", 0) * price_in
             + usage.get("cache_read_input_tokens", 0) * price_cached
             + usage.get("output_tokens", 0) * price_out)
    return round(total / 1_000_000, 6)


class UsageLog:
    def __init__(self, path: str | None):
        self.path, self.lock = path, threading.Lock()

    def write(self, record: dict) -> None:
        if not self.path:
            return
        with self.lock, open(self.path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(record) + "\n")


def merge_usage(total: dict, part: dict | None) -> None:
    for key in ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"):
        if part and part.get(key) is not None:
            total[key] = part[key]  # Anthropic events repeat cumulative counts; keep the latest


def make_handler(config: dict, log: UsageLog):
    token = os.environ.get(config.get("proxy_token_env") or "", "")

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):
            sys.stderr.write("glm-proxy: " + fmt % args + "\n")

        def reply(self, status: int, payload: dict) -> None:
            data = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def error(self, status: int, kind: str, message: str) -> None:
            self.reply(status, {"type": "error", "error": {"type": kind, "message": message}})

        def authorized(self) -> bool:
            if not token:
                return True
            sent = self.headers.get("x-api-key") or self.headers.get("Authorization", "").removeprefix("Bearer ")
            return sent == token

        def do_GET(self):
            if self.path.rstrip("/") in ("", "/health"):
                return self.reply(200, {"ok": True, "providers": [p["name"] for p in config["providers"]]})
            self.error(404, "not_found_error", "only /v1/messages and /health")

        def do_POST(self):
            if not self.path.split("?")[0].endswith("/v1/messages"):
                return self.error(404, "not_found_error", "only /v1/messages is proxied")
            if not self.authorized():
                return self.error(401, "authentication_error", "wrong proxy token")
            try:
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)))
            except (ValueError, TypeError):
                return self.error(400, "invalid_request_error", "request body must be valid JSON")
            if not isinstance(body, dict):
                return self.error(400, "invalid_request_error", "request body must be a JSON object")
            requested = body.get("model", "")
            body["model"] = map_model(requested, config["models"])
            effort = config.get("effort", {}).get(body["model"])
            if effort and not (body.get("output_config") or {}).get("effort"):
                body.setdefault("output_config", {})["effort"] = effort
            payload = json.dumps(body).encode()
            failures = []
            for provider in config["providers"]:
                key = os.environ.get(provider.get("key_env", ""), "")
                if not key:
                    failures.append(f"{provider['name']}: no key in ${provider.get('key_env')}")
                    continue
                upstream = self.open_upstream(provider, key, payload)
                if isinstance(upstream, str):
                    failures.append(f"{provider['name']}: {upstream}")
                    continue
                if isinstance(upstream, urllib.error.HTTPError):
                    detail = upstream.read()
                    if upstream.code in FALLBACK_STATUSES:
                        failures.append(f"{provider['name']}: HTTP {upstream.code}")
                        continue
                    self.send_response(upstream.code)  # a request error: report it, don't retry elsewhere
                    self.send_header("Content-Type", upstream.headers.get("Content-Type", "application/json"))
                    self.send_header("Content-Length", str(len(detail)))
                    self.end_headers()
                    self.wfile.write(detail)
                    return
                return self.relay(upstream, provider, requested, body)
            self.error(529, "overloaded_error", "no provider answered: " + "; ".join(failures))

        def open_upstream(self, provider: dict, key: str, payload: bytes):
            request = urllib.request.Request(provider["base_url"].rstrip("/") + "/v1/messages", data=payload,
                                             method="POST")
            request.add_header("Content-Type", "application/json")
            request.add_header("Authorization", f"Bearer {key}")
            request.add_header("anthropic-version", self.headers.get("anthropic-version", "2023-06-01"))
            if config.get("forward_beta_headers") and self.headers.get("anthropic-beta"):
                request.add_header("anthropic-beta", self.headers["anthropic-beta"])
            request.add_header("User-Agent", USER_AGENT)
            try:
                return urllib.request.urlopen(request, timeout=provider.get("timeout", 600))
            except urllib.error.HTTPError as error:
                return error
            except (urllib.error.URLError, socket.timeout, ConnectionError, http.client.HTTPException) as error:
                return f"network error: {getattr(error, 'reason', error)}"

        def relay(self, upstream, provider: dict, requested: str, body: dict) -> None:
            usage: dict = {}
            with upstream:
                kind = upstream.headers.get("Content-Type", "")
                self.send_response(upstream.status)
                self.send_header("Content-Type", kind or "application/json")
                if "text/event-stream" not in kind:
                    data = upstream.read()
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                    try:
                        merge_usage(usage, json.loads(data).get("usage"))
                    except ValueError:
                        pass
                else:
                    self.send_header("Cache-Control", "no-cache")
                    self.send_header("Transfer-Encoding", "chunked")
                    self.end_headers()
                    for raw in upstream:
                        self.wfile.write(f"{len(raw):x}\r\n".encode() + raw + b"\r\n")
                        self.wfile.flush()
                        line = raw.decode("utf-8", errors="replace").strip()
                        if line.startswith("data:"):
                            try:
                                event = json.loads(line[5:])
                            except ValueError:
                                continue
                            merge_usage(usage, (event.get("message") or {}).get("usage") or event.get("usage"))
                    self.wfile.write(b"0\r\n\r\n")
            log.write({
                "time": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
                "provider": provider["name"], "requested": requested, "model": body["model"],
                "effort": (body.get("output_config") or {}).get("effort"), "stream": bool(body.get("stream")),
                "usage": usage, "usd": cost(provider.get("prices", {}), body["model"], usage),
            })

    return Handler


def serve(config: dict, port: int | None = None) -> ThreadingHTTPServer:
    host, _, default_port = config["listen"].rpartition(":")
    if host not in ("127.0.0.1", "localhost", "::1"):
        raise SystemExit("refusing to listen beyond localhost: the proxy holds your provider keys")
    return ThreadingHTTPServer((host, int(default_port) if port is None else port),
                               make_handler(config, UsageLog(config.get("usage_log"))))


def report(path: str) -> str:
    rows: dict = {}
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            record = json.loads(line)
            key = (record["time"][:10], record["provider"], record["model"])
            row = rows.setdefault(key, {"calls": 0, "input": 0, "cached": 0, "output": 0, "usd": 0.0})
            usage = record.get("usage") or {}
            row["calls"] += 1
            row["input"] += usage.get("input_tokens", 0)
            row["cached"] += usage.get("cache_read_input_tokens", 0)
            row["output"] += usage.get("output_tokens", 0)
            row["usd"] += record.get("usd") or 0
    lines = ["| Day | Provider | Model | Calls | Input | Cached | Output | USD |", "|---|---|---|---:|---:|---:|---:|---:|"]
    for (day, provider, model), row in sorted(rows.items()):
        lines.append(f"| {day} | {provider} | {model} | {row['calls']} | {row['input']:,} | {row['cached']:,} | "
                     f"{row['output']:,} | ${row['usd']:.4f} |")
    return "\n".join(lines)


def claude_code_env(config: dict) -> str:
    host, _, port = config["listen"].rpartition(":")
    env = {
        "ANTHROPIC_BASE_URL": f"http://{host}:{port}",
        "ANTHROPIC_AUTH_TOKEN": "$" + (config.get("proxy_token_env") or "GLM_PROXY_TOKEN"),
        "CLAUDE_CODE_AUTO_COMPACT_WINDOW": "1000000",
    }
    return "\n".join(f"export {name}={value}" for name, value in env.items())


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Run Claude Code on GLM-5.3 through a local proxy.")
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("serve", help="start the proxy")
    run.add_argument("--config", help="JSON file; keys override the built-in defaults")
    run.add_argument("--port", type=int)
    sub.add_parser("env", help="print Claude Code environment variables for the proxy").add_argument("--config")
    sub.add_parser("report", help="summarise a usage log").add_argument("log")
    args = parser.parse_args(argv)
    if args.command == "report":
        print(report(args.log))
        return 0
    config = load_config(getattr(args, "config", None))
    if args.command == "env":
        print(claude_code_env(config))
        return 0
    server = serve(config, args.port)
    print(f"glm-proxy on http://{server.server_address[0]}:{server.server_address[1]} -> "
          + ", ".join(p["name"] for p in config["providers"]), file=sys.stderr)
    server.serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
