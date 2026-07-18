#!/usr/bin/env python3
"""Mock Chrome CDP endpoint for the browser-tunnel runtime integration test.

Serves a fixed `CDP-MOCK-OK` body on 127.0.0.1:9222 so the namespace-owner
container can verify reverse-tunnel traffic traversal via `curl/wget`.

This is a test fixture, not production code.
"""

import http.server
import socketserver
import sys


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        body = b"CDP-MOCK-OK\n"
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):  # silence default logging
        pass


def main() -> int:
    host, port = "127.0.0.1", 9222
    with socketserver.TCPServer((host, port), Handler) as httpd:
        print(f"mock-cdp listening on {host}:{port}", flush=True)
        httpd.serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())