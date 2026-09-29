"""Mock gateway + object storage for a manual cascade_mesh run.

GET  /get/<role>  -> serves <dir>/<file> (signed-URL download)
PUT  /put/out     -> saves the CityJSON to <dir>/output.city.json (presigned upload)
POST /callback    -> prints and logs the job callback to <dir>/callbacks.jsonl

usage: uv run python docs/lod2-testing/mock_world.py <dir> [port]   (stdlib only)
"""

import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(sys.argv[1]).resolve()
PORT = int(sys.argv[2]) if len(sys.argv) > 2 else 9000
FILES = {"bo": "bo.geojson", "rs": "rs.geojson", "dsm": "dsm.tif", "dtm": "dtm.tif"}


class Handler(BaseHTTPRequestHandler):
    def _body(self) -> bytes:
        return self.rfile.read(int(self.headers.get("Content-Length", 0)))

    def do_GET(self) -> None:
        role = self.path.split("?")[0].rsplit("/", 1)[-1]
        path = ROOT / FILES.get(role, "__missing__")
        if not path.exists():
            self.send_response(403)
            self.end_headers()
            return
        data = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_PUT(self) -> None:
        data = self._body()
        (ROOT / "output.city.json").write_bytes(data)
        print(f"PUT  output.city.json  {len(data)} bytes", flush=True)
        self.send_response(200)
        self.end_headers()

    def do_POST(self) -> None:
        event = json.loads(self._body())
        with (ROOT / "callbacks.jsonl").open("a", encoding="utf-8") as log:
            log.write(json.dumps(event) + "\n")
        print(
            f"CALLBACK seq={event.get('sequence')} status={event.get('status')} "
            f"progress={event.get('progress_percent')} error={event.get('error_message')}",
            flush=True,
        )
        body = b'{"received": true}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: object) -> None:
        pass


print(f"mock world on :{PORT}, serving {ROOT}", flush=True)
ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
