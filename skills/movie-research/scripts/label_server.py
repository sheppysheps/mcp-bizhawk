#!/usr/bin/env python3
"""label_server: a local page for naming the transitions in a movie capture.

  python label_server.py <export> --source room 0xC3EF 3 [--source ...] [--fades 0.5] [--port 8770]

Lists every transition (`movie_export events`), shows the screenshots before
and after, and saves what the person types to <export>/labels.json as they go.
Open the URL it prints (it contains the session token). Ctrl+C stops it.

Standard library only.

System access, and its limits (this is a server with write access, so each
part is fenced):
- Network: binds 127.0.0.1 only (never another interface), so nothing on the
  network can reach it. Every API request must carry the random token printed
  at startup, and the Host header must name this server, so other web pages
  open in the same browser can't read or write through it (CSRF, DNS
  rebinding).
- Files read: the page's own files in label_page/ next to this script, and
  screenshots in the export folder whose names match the recorder's pattern.
  No other path can be requested.
- Files written: only <export>/labels.json, replaced atomically, at most
  MAX_LABELS_BYTES, and only when the body is a JSON object of the expected
  shape.
"""

import argparse
import hmac
import json
import os
import secrets
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
import movie_export  # noqa: E402

PAGE_DIR = os.path.join(HERE, "label_page")
PAGE_FILES = {"/": ("index.html", "text/html; charset=utf-8"),
              "/label.js": ("label.js", "text/javascript; charset=utf-8"),
              "/label.css": ("label.css", "text/css; charset=utf-8")}
MAX_LABELS_BYTES = 4 * 1024 * 1024
LABEL_KEYS = {"version", "places", "events", "items"}
HEADERS = {
    # Scripts and styles only from this server, no inline code, no framing.
    "Content-Security-Policy": "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self'; "
                               "connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
}


# ── Pure helpers (tested without a server) ─────────────────────────────────

def host_allowed(host, port):
    """Is the Host header this server's own address?

    Returns: True for 127.0.0.1:<port> or localhost:<port>, else False (a
    page on another name pointed at 127.0.0.1, i.e. DNS rebinding).
    Requires: nothing.
    """
    return host in (f"127.0.0.1:{port}", f"localhost:{port}")


def shot_path(folder, name):
    """Resolve a requested screenshot name inside the export folder.

    Returns: the file path, or None unless `name` is a plain file name matching
    the recorder's screenshot pattern and the file exists.
    Requires: movie_export.SHOT_RE.
    """
    if not name or os.path.basename(name) != name or not movie_export.SHOT_RE.match(name):
        return None
    path = os.path.join(folder, name)
    return path if os.path.isfile(path) else None


def validate_labels(data):
    """Check a labels document before it's written.

    Returns: None if it's acceptable, else a short reason. Accepts a JSON
    object whose keys are among LABEL_KEYS, with "places" and "events"
    objects and "items" a list when present.
    Requires: nothing.
    """
    if not isinstance(data, dict):
        return "labels must be a JSON object"
    extra = set(data) - LABEL_KEYS
    if extra:
        return f"unexpected keys: {', '.join(sorted(extra))}"
    for key, kind in (("places", dict), ("events", dict), ("items", list)):
        if key in data and not isinstance(data[key], kind):
            return f"{key} must be a {'list' if kind is list else 'JSON object'}"
    return None


def load_labels(folder):
    """Read <folder>/labels.json. Returns: the labels dict, or an empty one. Requires: read access."""
    path = os.path.join(folder, "labels.json")
    if not os.path.exists(path):
        return {"version": 1, "places": {}, "events": {}, "items": []}
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def save_labels(folder, data):
    """Write <folder>/labels.json atomically (temp file, then replace).

    Returns: None. Requires: write access to the export folder only.
    """
    path = os.path.join(folder, "labels.json")
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        json.dump(data, f, indent=1, sort_keys=True)
    os.replace(tmp, path)


def build_events(folder, sources, fades=None, cluster=60, lag_min=5):
    """The events the page shows, with values as hex text for display.

    Returns: list of event dicts from movie_export.events, each change's
    "old"/"new" also given as "old_hex"/"new_hex" and "key" ("source:VALUE",
    the place key for that value).
    Requires: an export folder with writes.tsv covering the sources.
    """
    e = movie_export.load(folder)
    dark = None
    if fades is not None:
        shots = movie_export.list_shots(folder)
        median = movie_export.measure_shots(folder, shots)
        dark = [s["frame"] for s in shots if s["brightness"] <= median * fades]
    rows = movie_export.events(e, sources, cluster=cluster, lag_min=lag_min, fades=dark)
    for r in rows:
        for c in r["changes"]:
            width = 2 * c["width"]
            c["old_hex"], c["new_hex"] = f"{c['old']:0{width}X}", f"{c['new']:0{width}X}"
            c["key"] = f"{c['source']}:{c['new_hex']}"
    return rows


# ── Server ────────────────────────────────────────────────────────────────

def make_handler(folder, token, events, port_ref):
    """Build the request handler class bound to one export folder and token.

    Returns: a BaseHTTPRequestHandler subclass. `port_ref` is a one-item list
    holding the bound port (known only after binding).
    Requires: the page files in PAGE_DIR.
    """

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):  # quiet: no per-request lines
            pass

        def _send(self, code, body, ctype="application/json; charset=utf-8"):
            if isinstance(body, (dict, list)):
                body = json.dumps(body).encode("utf-8")
            elif isinstance(body, str):
                body = body.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            for k, v in HEADERS.items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def _checked(self):
            """Host and token checks. Returns: (path, query) or None after sending an error."""
            if not host_allowed(self.headers.get("Host", ""), port_ref[0]):
                self._send(403, {"error": "wrong host"})
                return None
            url = urlparse(self.path)
            if url.path.startswith("/api/"):
                given = (parse_qs(url.query).get("token") or [""])[0]
                if not hmac.compare_digest(given, token):
                    self._send(403, {"error": "missing or wrong token"})
                    return None
            return url.path, url.query

        def do_GET(self):
            checked = self._checked()
            if not checked:
                return
            path, _ = checked
            if path in PAGE_FILES:
                name, ctype = PAGE_FILES[path]
                with open(os.path.join(PAGE_DIR, name), "rb") as f:
                    self._send(200, f.read(), ctype)
            elif path == "/api/events":
                self._send(200, {"folder": os.path.basename(folder), "events": events, "labels": load_labels(folder)})
            elif path.startswith("/shots/"):
                file = shot_path(folder, path[len("/shots/"):])
                if not file:
                    self._send(404, {"error": "no such screenshot"})
                    return
                with open(file, "rb") as f:
                    self._send(200, f.read(), "image/png")
            else:
                self._send(404, {"error": "not found"})

        def do_POST(self):
            # Read the body first (bounded), even for requests about to be
            # refused: answering before it is read makes Windows reset the
            # connection, and the client sees an abort instead of the error.
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                length = -1
            if length <= 0 or length > MAX_LABELS_BYTES:
                self.close_connection = True
                self._send(413, {"error": "empty or too large"})
                return
            raw = self.rfile.read(length)
            checked = self._checked()
            if not checked:
                return
            path, _ = checked
            if path != "/api/labels":
                self._send(404, {"error": "not found"})
                return
            # JSON only: a cross-site form can't send this content type without
            # a CORS preflight, which this server never answers.
            if self.headers.get("Content-Type", "").split(";")[0].strip() != "application/json":
                self._send(415, {"error": "send application/json"})
                return
            try:
                data = json.loads(raw.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                self._send(400, {"error": "not JSON"})
                return
            problem = validate_labels(data)
            if problem:
                self._send(400, {"error": problem})
                return
            save_labels(folder, data)
            self._send(200, {"saved": True})

    return Handler


def serve(folder, sources, port=8770, fades=None, token=None):
    """Start the server (blocking until Ctrl+C) on 127.0.0.1 only.

    Returns: None. Prints the URL with the session token.
    Requires: an export folder; a free port (0 picks one).
    """
    token = token or secrets.token_urlsafe(24)
    events = build_events(folder, sources, fades=fades)
    port_ref = [port]
    server = ThreadingHTTPServer(("127.0.0.1", port), make_handler(folder, token, events, port_ref))
    port_ref[0] = server.server_address[1]
    print(f"{len(events)} events from {folder}")
    print(f"open http://127.0.0.1:{port_ref[0]}/?token={token}")
    print("labels are saved to labels.json in the export folder as you type; Ctrl+C stops the server")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


def main(argv=None):
    """Parse arguments and start the server. Returns: exit code."""
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("export")
    p.add_argument("--source", nargs=3, action="append", required=True, metavar=("NAME", "ADDRESS", "WIDTH"),
                   help="a watched variable that changes at transitions, e.g. room 0xC3EF 3 (repeatable)")
    p.add_argument("--fades", type=float, metavar="FRACTION", help="also list dark screenshots (see events --fades)")
    p.add_argument("--port", type=int, default=8770, help="port on 127.0.0.1 (0 picks a free one)")
    args = p.parse_args(argv)
    if not os.path.exists(os.path.join(args.export, "frames.tsv")):
        print(f"{args.export}: not a movie_recorder export folder")
        return 1
    sources = [(name, int(addr, 0), int(width, 0)) for name, addr, width in args.source]
    serve(args.export, sources, port=args.port, fades=args.fades)
    return 0


if __name__ == "__main__":
    sys.exit(main())
