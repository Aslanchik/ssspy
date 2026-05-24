#!/usr/bin/env python3
"""Capture a single OTLP HTTP request body to a file.

Stand up a minimal HTTP server, accept exactly one ``POST /v1/traces`` (or
``GET /health``), write the body to the requested path, return a valid
``ExportTraceServiceResponse``, and exit.

Usage:
    python scripts/capture_fixtures.py --out tests/fixtures/full.otlp.bin [--port 4318]

Then run go-phish with ``OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:<port>``.
"""
from __future__ import annotations

import argparse
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
    ExportTraceServiceResponse,
)


class _CaptureHandler(BaseHTTPRequestHandler):
    out_path: Path = Path()
    stop_event: threading.Event = threading.Event()
    received: bool = False

    def log_message(self, fmt: str, *args: object) -> None:  # noqa: D401 - stdlib API
        # Quieter than the default.
        sys.stderr.write("[capture] " + fmt % args + "\n")

    def do_GET(self) -> None:  # noqa: N802 - stdlib API
        if self.path == "/health":
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"status":"ok"}')
            return
        self.send_error(404)

    def do_POST(self) -> None:  # noqa: N802 - stdlib API
        if self.path != "/v1/traces":
            self.send_error(404)
            return
        length = int(self.headers.get("content-length", "0"))
        body = self.rfile.read(length)
        _CaptureHandler.out_path.parent.mkdir(parents=True, exist_ok=True)
        _CaptureHandler.out_path.write_bytes(body)
        resp = ExportTraceServiceResponse()
        payload = resp.SerializeToString()
        self.send_response(200)
        self.send_header("content-type", "application/x-protobuf")
        self.send_header("content-length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)
        _CaptureHandler.received = True
        _CaptureHandler.stop_event.set()


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--out", required=True, type=Path, help="output path for the captured body")
    p.add_argument("--port", default=4318, type=int, help="listen port (default 4318)")
    p.add_argument(
        "--host", default="127.0.0.1", help="listen address (default 127.0.0.1)"
    )
    args = p.parse_args(argv)

    _CaptureHandler.out_path = args.out
    _CaptureHandler.stop_event = threading.Event()

    server = HTTPServer((args.host, args.port), _CaptureHandler)
    print(f"[capture] listening on {args.host}:{args.port}, writing to {args.out}")
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    try:
        _CaptureHandler.stop_event.wait()
    except KeyboardInterrupt:
        pass
    server.shutdown()
    t.join(timeout=2)
    if _CaptureHandler.received:
        print(f"[capture] wrote {args.out} ({args.out.stat().st_size} bytes)")
        return 0
    print("[capture] no request received")
    return 1


if __name__ == "__main__":
    sys.exit(main())
