"""Tiny local receiver for ACS Call Automation events (debugging only).

Prints each event's type and result info (the code/subCode that explains why
a call failed or ended), and appends the raw JSON to calls/events.log.
Expose it with:  cloudflared tunnel --url http://localhost:8080

Usage: python3 calls/event_server.py
"""
import json
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

LOG = Path(__file__).parent / "events.log"


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        self.send_response(200)
        self.end_headers()
        try:
            events = json.loads(body)
        except ValueError:
            return
        with LOG.open("a") as f:
            for ev in events if isinstance(events, list) else [events]:
                f.write(json.dumps(ev) + "\n")
                data = ev.get("data", {})
                info = data.get("resultInformation") or {}
                print(f"{datetime.now():%H:%M:%S} {ev.get('type', '?').split('.')[-1]:<28} "
                      f"call={data.get('callConnectionId', '')[:8]} "
                      f"code={info.get('code')} sub={info.get('subCode')} {info.get('message', '')}",
                      flush=True)

    def log_message(self, *args):
        pass


if __name__ == "__main__":
    print("listening on :8080", flush=True)
    HTTPServer(("127.0.0.1", 8080), Handler).serve_forever()
