"""Unit tests for label_server.py. Standard library only.

Run from the repo root:  python -m unittest discover -s skills/movie-research/tests -v
"""

import http.client
import json
import os
import sys
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "scripts"))
sys.path.insert(0, HERE)

import label_server  # noqa: E402
from test_movie_export import write_export  # noqa: E402


class Helpers(unittest.TestCase):
    def test_host_allowed(self):
        self.assertTrue(label_server.host_allowed("127.0.0.1:8770", 8770))
        self.assertTrue(label_server.host_allowed("localhost:8770", 8770))
        self.assertFalse(label_server.host_allowed("evil.example:8770", 8770))
        self.assertFalse(label_server.host_allowed("127.0.0.1:9999", 8770))

    def test_shot_path_only_allows_screenshots_in_the_folder(self):
        with tempfile.TemporaryDirectory() as d:
            open(os.path.join(d, "shot_f0000100_every.png"), "wb").close()
            open(os.path.join(d, "labels.json"), "w").close()
            self.assertTrue(label_server.shot_path(d, "shot_f0000100_every.png"))
            for bad in ("labels.json", "../shot_f0000100_every.png", "sub/shot_f0000100_every.png",
                        "shot_f0000200_every.png", ""):
                self.assertIsNone(label_server.shot_path(d, bad), bad)

    def test_requires_vocabulary_is_kept_and_extended(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertNotIn("vocab", label_server.load_labels(d))
            label_server.save_labels(d, {"version": 1, "places": {}, "events": {}, "vocab": {"requires": ["key"]}})
            data = label_server.load_labels(d, ["double jump", "key"])
            self.assertEqual(data["vocab"]["requires"], ["key", "double jump"])

    def test_labels_without_a_computed_event_become_manual_events(self):
        events = [{"frame": 100, "end": 100, "changes": [], "lag": 8, "guess": "load"}]
        shots = [{"frame": 90, "file": "a.png"}, {"frame": 150, "file": "b.png"}]
        labels = {"events": {"100": {"kind": "door"}, "120": {"notes": "added by hand"}, "x": {}}}
        out = label_server.with_manual_events(events, labels, shots)
        self.assertEqual([(e["frame"], e["guess"]) for e in out], [(100, "load"), (120, "manual")])
        self.assertEqual((out[1]["shot_before"], out[1]["shot_after"]), ("a.png", "b.png"))

    def test_validate_labels(self):
        self.assertIsNone(label_server.validate_labels({"version": 1, "places": {}, "events": {}, "items": []}))
        self.assertIn("object", label_server.validate_labels([]))
        self.assertIn("unexpected", label_server.validate_labels({"places": {}, "script": "x"}))
        self.assertIn("items", label_server.validate_labels({"items": {}}))


class Server(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.folder = write_export(self.tmp.name, frames=300, lag_frames=range(102, 110),
                                   writes=[(1, 0xC000, 0, 1, "init"), (100, 0xC000, 1, 2, "0x5FEF")])
        with open(os.path.join(self.folder, "shot_f0000090_every.png"), "wb") as f:
            f.write(b"\x89PNG fake")
        events = label_server.build_events(self.folder, [("room", 0xC000, 1)])
        port_ref = [0]
        self.server = ThreadingHTTPServer(("127.0.0.1", 0),
                                          label_server.make_handler(self.folder, "tok", events, port_ref))
        port_ref[0] = self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def request(self, method, path, body=None, host=None, ctype="application/json"):
        """One request. Returns: (status, body bytes, headers)."""
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        headers = {"Host": host or f"127.0.0.1:{self.port}"}
        if body is not None:
            headers["Content-Type"] = ctype
        c.request(method, path, body=body, headers=headers)
        r = c.getresponse()
        data = r.read()
        c.close()
        return r.status, data, dict(r.getheaders())

    def test_page_events_and_shots(self):
        status, body, headers = self.request("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn(b"Labeling Station", body)
        self.assertIn("script-src 'self'", headers["Content-Security-Policy"])
        status, body, _ = self.request("GET", "/api/events?token=tok")
        data = json.loads(body)
        self.assertEqual([(e["frame"], e["guess"]) for e in data["events"]], [(100, "load")])
        self.assertEqual(data["events"][0]["changes"][0]["key"], "room:02")
        self.assertEqual(data["door_presets"], [])
        self.assertEqual(self.request("GET", "/shots/shot_f0000090_every.png")[0], 200)
        self.assertEqual(self.request("GET", "/shots/..%2Ffrows.tsv")[0], 404)
        self.assertEqual(self.request("GET", "/shots/frames.tsv")[0], 404)

    def test_token_host_and_content_type_are_enforced(self):
        self.assertEqual(self.request("GET", "/api/events")[0], 403)
        self.assertEqual(self.request("GET", "/api/events?token=nope")[0], 403)
        self.assertEqual(self.request("GET", "/api/events?token=tok", host=f"evil.example:{self.port}")[0], 403)
        body = json.dumps({"places": {"room:02": {"name": "Town"}}})
        self.assertEqual(self.request("POST", "/api/labels", body)[0], 403)
        self.assertEqual(self.request("POST", "/api/labels?token=tok", body, ctype="text/plain")[0], 415)
        self.assertEqual(self.request("POST", "/api/labels?token=tok", json.dumps({"x": 1}))[0], 400)
        self.assertFalse(os.path.exists(os.path.join(self.folder, "labels.json")))

    def test_labels_are_saved_and_served_back(self):
        body = json.dumps({"version": 1, "places": {"room:02": {"name": "Town"}}, "events": {"100": {"kind": "door"}}})
        status, data, _ = self.request("POST", "/api/labels?token=tok", body)
        self.assertEqual(status, 200)
        with open(os.path.join(self.folder, "labels.json"), encoding="utf-8") as f:
            self.assertEqual(json.load(f)["places"]["room:02"]["name"], "Town")
        data = json.loads(self.request("GET", "/api/events?token=tok")[1])
        self.assertEqual(data["labels"]["events"]["100"]["kind"], "door")


if __name__ == "__main__":
    unittest.main()
