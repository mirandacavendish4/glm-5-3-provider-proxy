import json
import os
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from glm_proxy import DEFAULT_CONFIG, cost, map_model, serve

def test_model_mapping_strips_long_context_suffix():
    assert map_model("claude-haiku-5[1m]", DEFAULT_CONFIG["models"]) == "glm-5.3-flash"
    assert map_model("claude-opus-5", DEFAULT_CONFIG["models"]) == "glm-5.3"

def test_usage_cost_uses_cached_rate_separately():
    assert cost({"glm-5.3": [1.12, .208, 3.52]}, "glm-5.3", {"input_tokens": 1000000, "cache_read_input_tokens": 100000, "output_tokens": 1000000}) == 4.6608

def test_server_only_accepts_loopback():
    config={**DEFAULT_CONFIG,"listen":"127.0.0.1:0"}
    server=serve(config)
    try: assert server.server_address[0] == "127.0.0.1"
    finally: server.server_close()


def test_messages_are_mapped_and_forwarded_to_a_mock_provider(tmp_path):
    received = []
    class Provider(BaseHTTPRequestHandler):
        def do_POST(self):
            received.append((json.loads(self.rfile.read(int(self.headers["Content-Length"]))), self.headers.get("User-Agent")))
            payload = json.dumps({"type": "message", "id": "msg_1", "usage": {
                "input_tokens": 2, "output_tokens": 3}, "content": []}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers(); self.wfile.write(payload)
        def log_message(self, *args):
            pass
    provider = ThreadingHTTPServer(("127.0.0.1", 0), Provider)
    provider_thread = threading.Thread(target=provider.serve_forever, daemon=True); provider_thread.start()
    config = {**DEFAULT_CONFIG, "listen": "127.0.0.1:0", "usage_log": str(tmp_path / "usage.jsonl"),
              "providers": [{"name": "mock", "base_url": f"http://127.0.0.1:{provider.server_address[1]}",
                              "key_env": "MOCK_KEY", "prices": {"glm-5.3": [1, 0, 3]}}]}
    old = os.environ.get("MOCK_KEY"); os.environ["MOCK_KEY"] = "mock-key"
    proxy = serve(config); proxy_thread = threading.Thread(target=proxy.serve_forever, daemon=True); proxy_thread.start()
    try:
        req = urllib.request.Request(f"http://127.0.0.1:{proxy.server_address[1]}/v1/messages",
                                     data=json.dumps({"model": "claude-opus-5", "messages": []}).encode(), method="POST",
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req) as response:
            assert response.status == 200
        assert received[0][0]["model"] == "glm-5.3"
        assert received[0][1] == "glm-5-3-provider-proxy/0.1"
        for _ in range(20):
            if (tmp_path / "usage.jsonl").exists():
                break
            time.sleep(0.01)
        assert "| mock | glm-5.3 |" in __import__("glm_proxy").report(str(tmp_path / "usage.jsonl"))
    finally:
        proxy.shutdown(); proxy.server_close(); provider.shutdown(); provider.server_close()
        if old is None: os.environ.pop("MOCK_KEY", None)
        else: os.environ["MOCK_KEY"] = old
