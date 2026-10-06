"""Shared deterministic mock model transport for DSH integration checks."""

from __future__ import annotations

import json
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

import pytest


@pytest.fixture
def mock_dsh_tool_provider():
    @contextmanager
    def serve(tool_name: str | None, arguments: dict, final_response: str):
        requests: list[dict] = []

        class Provider(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                if self.path != "/v1/chat/completions":
                    self.send_error(404)
                    return
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                requests.append(body)
                request_number = len(requests)
                chunks = [
                    {
                        "id": f"tool-mock-{request_number}",
                        "object": "chat.completion.chunk",
                        "created": 0,
                        "model": "mock-model",
                        "choices": [
                            {"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}
                        ],
                    }
                ]
                if request_number == 1 and tool_name is not None:
                    delta = {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "mock-tool-call",
                                "type": "function",
                                "function": {
                                    "name": tool_name,
                                    "arguments": json.dumps(arguments),
                                },
                            }
                        ]
                    }
                    finish_reason = "tool_calls"
                else:
                    delta = {"content": final_response}
                    finish_reason = "stop"
                chunks.extend(
                    [
                        {
                            "id": f"tool-mock-{request_number}",
                            "object": "chat.completion.chunk",
                            "created": 0,
                            "model": "mock-model",
                            "choices": [{"index": 0, "delta": delta, "finish_reason": None}],
                        },
                        {
                            "id": f"tool-mock-{request_number}",
                            "object": "chat.completion.chunk",
                            "created": 0,
                            "model": "mock-model",
                            "choices": [{"index": 0, "delta": {}, "finish_reason": finish_reason}],
                        },
                    ]
                )
                payload = (
                    "".join(f"data: {json.dumps(chunk)}\n\n" for chunk in chunks)
                    + "data: [DONE]\n\n"
                ).encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream; charset=utf-8")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, _format: str, *_args: object) -> None:
                pass

        with ThreadingHTTPServer(("127.0.0.1", 0), Provider) as server:
            thread = Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                yield f"http://127.0.0.1:{server.server_port}/v1", requests
            finally:
                server.shutdown()
                thread.join(timeout=5)

    return serve
