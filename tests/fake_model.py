"""A tiny local stand-in for an OpenAI-compatible model server, used by tests."""
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer


class FakeModel:
    """Starts a tiny local server that answers /v1/chat/completions with a canned reply."""

    def __init__(self, reply, status: int = 200, reject_json_mode: bool = False):
        """reply: a string, or a function(request_body) -> string to answer each request differently."""
        self.reply, self.status, self.reject_json_mode, self.requests = reply, status, reject_json_mode, []
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                fake.requests.append(body)
                if fake.reject_json_mode and "response_format" in body:
                    self.send_response(400); self.end_headers(); return
                self.send_response(fake.status)
                self.send_header("Content-Type", "application/json"); self.end_headers()
                text = fake.reply(body) if callable(fake.reply) else fake.reply
                self.wfile.write(json.dumps({"choices": [{"message": {"content": text}}]}).encode())

            def log_message(self, *a):
                pass

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_port}/v1"

    def close(self):
        self.server.shutdown()
