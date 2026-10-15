#!/usr/bin/env python3
"""A stand-in for Ollama (port 11434) and an OpenAI-compatible server (port
6767) for examples/compare.sh, which runs it in a private network namespace.

The reply is chosen by the first word of the user's latest own message (a
"System Command Execution Results" message continues the script of the
message before it). Every request is logged to $FAKE_HTTP_LOG, its JSON body
with sorted keys, so both widgets' requests can be compared."""

import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

RESULTS = "System Command Execution Results"

# keyword -> (content chunks, reasoning chunks); "KEYWORD:after" answers the
# command results.
SCRIPTS = {
    "hello": ([
        "Hello", " there! Here is **bold**, *italic*, `code` and a list:\n",
        "- one\n- two\n\n## Heading\n1. first\n2. second\n\n```python\nprint('hi')\n```",
    ], []),
    "think": (["The answer ", "is **42**."], ["Let me think ", "about the question ", "carefully."]),
    "inline": (["<think>pondering the ", "question</think>", "Done thinking."], []),
    "reasonly": ([], ["Only reasoning, ", "no answer."]),
    "run": (["```bash\necho alexy-out\nls /nonexistent-alexy\n```"], []),
    "run:after": (["The command printed **alexy-out**."], []),
    "two": (["First:\n```bash\necho one\n```\nthen:\n```sh\necho two; exit 3\n```"], []),
    "two:after": (["Both ran."], []),
    "sudo": (["```bash\nsudo pacman -Syu --noconfirm\n```"], []),
    "sudo:after": (["The system is up to date."], []),
    "reboot": (["```bash\nsudo reboot\n```"], []),
    "reboot:after": (["Rebooting."], []),
    "long": (["# Title\n\n" + "\n\n".join(
        f"Paragraph {n}: some text that is long enough to wrap around the chat bubble "
        "at least once or twice, with a `command` and **bold words** in it."
        for n in range(1, 9))], []),
    "slow": (["Slow ", "reply ", "that ", "takes ", "a ", "while."], []),
    "polish": (["Cześć! Zażółć gęślą jaźń — ünïcödé ✓."], []),
}


def log(line):
    path = os.environ.get("FAKE_HTTP_LOG")
    if path:
        with open(path, "a", encoding="utf-8") as file:
            file.write(line + "\n")


def text_of(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(item.get("text", "") for item in content
                        if isinstance(item, dict) and item.get("type") == "text")
    return ""


def keyword(messages):
    users = [text_of(m.get("content")) for m in messages if m.get("role") == "user"]
    if not users:
        return ""
    after = users[-1].startswith(RESULTS)
    own = [u for u in users if not u.startswith(RESULTS)]
    if not own:
        return ""
    text = own[-1]
    # Screen Awareness puts its instructions before the user's words.
    if text.startswith("[A screenshot"):
        text = text.split("]\n\n", 1)[-1]
    words = text.strip().split()
    word = words[0].lower().strip("!?.,") if words else ""
    return f"{word}:after" if after else word


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def send_body(self, status, ctype, body):
        data = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(data)
        self.close_connection = True

    def stream(self, ctype, pieces, delay):
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Transfer-Encoding", "chunked")
        self.send_header("Connection", "close")
        self.end_headers()
        try:
            for piece in pieces:
                data = piece.encode("utf-8")
                self.wfile.write(f"{len(data):x}\r\n".encode() + data + b"\r\n")
                self.wfile.flush()
                if delay:
                    time.sleep(delay)
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()
        except OSError:
            pass
        self.close_connection = True

    def do_GET(self):
        log(f"GET {self.server.server_port}{self.path}")
        if self.path.endswith("/models") and self.server.server_port == 6767:
            if os.environ.get("FAKE_NO_MODELS"):
                self.send_body(500, "application/json", '{"error": "no models"}')
            else:
                self.send_body(200, "application/json",
                               '{"object": "list", "data": [{"id": "fake-endpoint-model", "object": "model"}]}')
            return
        self.send_body(404, "text/plain", "404 page not found")

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length)
        try:
            body = json.loads(raw)
        except ValueError:
            body = {"unparsed": raw.decode("utf-8", "replace")}
        auth = self.headers.get("Authorization")
        log(f"POST {self.server.server_port}{self.path} auth={auth} "
            f"type={self.headers.get('Content-Type')} "
            f"body={json.dumps(body, sort_keys=True, ensure_ascii=False)}")
        messages = body.get("messages") or []
        word = keyword(messages)
        content, reasoning = SCRIPTS.get(word, ([f"I do not know {word!r}."], []))
        delay = 0.4 if word == "slow" else 0.02
        ollama = self.path == "/api/chat"

        if word == "fail":
            self.send_body(500, "application/json", '{"error": "the server broke"}')
            return
        pulled = os.path.exists(os.path.join(os.environ.get("FAKE_STATE", "/nonexistent"),
                                             f"pulled-{body.get('model')}"))
        if ollama and body.get("model") == "missing-model" and not pulled:
            self.send_body(404, "application/json", '{"error": "model \\"missing-model\\" not found, try pulling it first"}')
            return
        if ollama and word == "oerror":
            self.stream("application/x-ndjson", [
                json.dumps({"message": {"role": "assistant", "content": "Par"}, "done": False}) + "\n",
                json.dumps({"error": "model runner has unexpectedly stopped"}) + "\n",
            ], 0.02)
            return
        if word == "plain":
            self.send_body(200, "application/json", json.dumps(
                {"choices": [{"message": {"role": "assistant", "content": "A **whole** JSON reply.",
                                          "reasoning_content": "Thought once."}}]}))
            return
        if word == "weird":
            self.send_body(200, "application/json", json.dumps({"error": {"message": "quota exceeded"}}))
            return

        if ollama:
            pieces = []
            for r in reasoning:
                pieces.append(json.dumps({"message": {"role": "assistant", "content": "", "thinking": r},
                                          "done": False}) + "\n")
            for c in content:
                pieces.append(json.dumps({"message": {"role": "assistant", "content": c}, "done": False}) + "\n")
            pieces.append(json.dumps({"message": {"role": "assistant", "content": ""}, "done": True}) + "\n")
            # Anything after done is ignored.
            pieces.append(json.dumps({"message": {"role": "assistant", "content": "IGNORED"}, "done": False}) + "\n")
            self.stream("application/x-ndjson", pieces, delay)
        else:
            pieces = [": keep-alive\n\n"]
            for r in reasoning:
                pieces.append("data: " + json.dumps({"choices": [{"delta": {"reasoning_content": r}}]}) + "\n\n")
            for c in content:
                pieces.append("data: " + json.dumps({"choices": [{"delta": {"content": c}}]}) + "\n\n")
            pieces.append("data: " + json.dumps({"choices": []}) + "\n\n")
            pieces.append("data: [DONE]\n\n")
            pieces.append("data: " + json.dumps({"choices": [{"delta": {"content": "IGNORED"}}]}) + "\n\n")
            self.stream("text/event-stream; charset=utf-8", pieces, delay)


def serve(port):
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.daemon_threads = True
    server.serve_forever()


if __name__ == "__main__":
    for port in (11434, 6767):
        threading.Thread(target=serve, args=(port,), daemon=True).start()
    print("ready", flush=True)
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        sys.exit(0)
