#!/usr/bin/env python3
"""Record DSH requests and enforce one shared evaluation sampling policy."""
import argparse
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
from pathlib import Path
import time
import urllib.error
import urllib.request
import uuid


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--upstream", required=True)
    ap.add_argument("--model", required=True)
    ap.add_argument("--api-key-file", type=Path)
    ap.add_argument("--port", type=int, default=19260)
    ap.add_argument("--output", type=Path, required=True)
    args = ap.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    key = args.api_key_file.read_text().strip() if args.api_key_file else "local"

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *_):
            pass

        def reply(self, status, body):
            data = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            if self.path.rstrip("/") == "/v1/models":
                return self.reply(200, {"object": "list", "data": [{"id": "benchmark-model", "object": "model"}]})
            return self.reply(404, {"error": "not found"})

        def do_POST(self):
            if self.path.rstrip("/") != "/v1/chat/completions":
                return self.reply(404, {"error": "not found"})
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            body.update(model=args.model, temperature=1.0, top_p=0.95, top_k=20, seed=16)
            body["chat_template_kwargs"] = {**body.get("chat_template_kwargs", {}), "enable_thinking": True}
            body["max_tokens"] = min(int(body.pop("max_completion_tokens", body.get("max_tokens", 16384))), 16384)
            directory = args.output / (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:8])
            directory.mkdir()
            (directory / "request.json").write_text(json.dumps(body, ensure_ascii=False, indent=2))
            started = time.perf_counter()
            req = urllib.request.Request(args.upstream.rstrip("/") + "/v1/chat/completions", data=json.dumps(body).encode(),
                                         headers={"Content-Type": "application/json", "Authorization": "Bearer " + key})
            try:
                upstream = urllib.request.urlopen(req, timeout=1300)
            except urllib.error.HTTPError as error:
                raw = error.read()
                (directory / "error.json").write_bytes(raw)
                self.send_response(error.code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)
                return
            with upstream, (directory / "response.raw").open("wb") as log:
                self.send_response(upstream.status)
                self.send_header("Content-Type", upstream.headers.get("Content-Type", "application/json"))
                self.send_header("Connection", "close")
                self.end_headers()
                first = None
                try:
                    # read1 flushes each available stream chunk without waiting for a full buffer.
                    while chunk := upstream.read1(65536):
                        if first is None:
                            first = time.perf_counter() - started
                        log.write(chunk)
                        log.flush()
                        self.wfile.write(chunk)
                        self.wfile.flush()
                finally:
                    self.close_connection = True
                    (directory / "timing.json").write_text(json.dumps({"wall_seconds": time.perf_counter() - started,
                                                                      "first_upstream_bytes_seconds": first}))
    HTTPServer(("127.0.0.1", args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
