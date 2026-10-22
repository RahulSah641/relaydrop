"""End-to-end API tests (Flask test client, temporary data folder).  Run:  python -m unittest discover -s tests -v"""
import io
import json
import re
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # backend/
from relaydrop import create_app  # noqa: E402
from relaydrop.core import safe_names, sha256_hex  # noqa: E402


class RelayDropTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.app = create_app({"TESTING": True, "DATA_DIR": Path(self.tmp.name), "FILES_DIR": Path(self.tmp.name) / "files",
                               "DB_PATH": Path(self.tmp.name) / "t.db", "ADMIN_KEY_HASH": sha256_hex("secret-key")})
        from relaydrop.core import init_db
        init_db(self.app)
        self.c = self.app.test_client()
        self.token = self.c.get("/api/session").get_json()["sender_token"]
        self.h = {"X-Relay-Sender": self.token}

    def tearDown(self):
        self.tmp.cleanup()

    def make_transfer(self, files=(("a.txt", b"hello"), ("b.bin", b"\x00\x01\x02")), password="", hours=24):
        r = self.c.post("/api/transfers", headers=self.h, json={"files": [{"name": n, "size": len(d), "type": "text/plain"} for n, d in files],
                                                              "hours": hours, "title": "T", "note": "N", "password": password})
        self.assertEqual(r.status_code, 201, r.get_data(as_text=True))
        draft = r.get_json()
        for (n, d), f in zip(files, draft["files"]):
            up = self.c.put(f"/api/transfers/{draft['id']}/files/{f['id']}", headers=self.h, data=d, content_type="application/octet-stream")
            self.assertEqual(up.status_code, 200, up.get_data(as_text=True))
        fin = self.c.post(f"/api/transfers/{draft['id']}/finalize", headers=self.h)
        self.assertEqual(fin.status_code, 200, fin.get_data(as_text=True))
        return fin.get_json()

    def test_health_and_pages(self):
        self.assertEqual(self.c.get("/health").get_json()["status"], "ok")
        for path in ["/", "/about", "/privacy", "/terms", "/copyright", "/contact", "/report", "/guides", "/admin",
                     "/guides/send-files-safely", "/guides/choose-transfer-expiry", "/guides/receive-files-and-fix-downloads"]:
            self.assertEqual(self.c.get(path).status_code, 200, path)
        self.assertEqual(self.c.get("/nope").status_code, 404)
        self.assertEqual(self.c.get("/guides/nope").status_code, 404)
        self.assertEqual(self.c.get("/about.html").status_code, 200)
        self.assertEqual(self.c.get("/css/style.css").status_code, 200)
        self.assertEqual(self.c.get("/../backend/run.py").status_code, 404)
        self.assertIn(b"RelayDrop", self.c.get("/nope").data)

    def test_local_dev_ports_allowed(self):
        r = self.c.get("/api/session", headers={"Origin": "http://localhost:5500"}, base_url="http://localhost:5000")
        self.assertEqual(r.headers.get("Access-Control-Allow-Origin"), "http://localhost:5500")
        r = self.c.get("/api/session", headers={"Origin": "http://evil.example"}, base_url="http://localhost:5000")
        self.assertIsNone(r.headers.get("Access-Control-Allow-Origin"))

    def test_cors_only_for_listed_origins(self):
        self.app.config["CORS_ORIGINS"] = ("https://me.github.io",)
        ok = self.c.options("/api/transfers", headers={"Origin": "https://me.github.io", "Access-Control-Request-Method": "POST"})
        self.assertEqual(ok.headers.get("Access-Control-Allow-Origin"), "https://me.github.io")
        post = self.c.post("/api/receive", headers={**self.h, "Origin": "https://me.github.io"}, json={"reference": "00000000"})
        self.assertEqual(post.status_code, 404)  # reached the handler (not blocked as cross-site)
        evil = self.c.post("/api/receive", headers={**self.h, "Origin": "https://evil.example"}, json={"reference": "00000000"})
        self.assertEqual(evil.status_code, 403)
        self.assertIsNone(evil.headers.get("Access-Control-Allow-Origin"))

    def test_full_flow_receive_zip_and_single(self):
        t = self.make_transfer()
        self.assertEqual(len(t["code"]), 8)
        r = self.c.post("/api/receive", headers=self.h, json={"reference": t["code"][:4] + " " + t["code"][4:]})
        self.assertEqual(r.status_code, 200)
        data = r.get_json()
        self.assertEqual([f["name"] for f in data["files"]], ["a.txt", "b.bin"])
        tok = data["download_token"]
        one = self.c.get(f"/download?token={tok}&file_id={data['files'][0]['id']}")
        self.assertEqual(one.data, b"hello")
        self.assertIn("attachment", one.headers["Content-Disposition"])
        z = self.c.get(f"/download?token={tok}")
        self.assertEqual(z.headers["Content-Type"], "application/zip")
        zf = zipfile.ZipFile(io.BytesIO(z.data))
        self.assertIsNone(zf.testzip())
        self.assertEqual(zf.read("a.txt"), b"hello")
        self.assertEqual(zf.read("b.bin"), b"\x00\x01\x02")
        # by share link
        link = f"http://localhost/?receive={t['share_token']}"
        self.assertEqual(self.c.post("/api/receive", headers=self.h, json={"reference": link}).status_code, 200)
        hist = self.c.get("/api/transfers", headers=self.h).get_json()["transfers"]
        self.assertEqual(hist[0]["download_count"], 2)

    def test_password_protection(self):
        t = self.make_transfer(password="correct-horse")
        self.assertTrue(t["protected"])
        r = self.c.post("/api/receive", headers=self.h, json={"reference": t["code"]})
        self.assertEqual(r.status_code, 401)
        self.assertTrue(r.get_json()["needs_password"])
        self.assertEqual(self.c.post("/api/receive", headers=self.h, json={"reference": t["code"], "password": "wrong"}).status_code, 401)
        self.assertEqual(self.c.post("/api/receive", headers=self.h, json={"reference": t["code"], "password": "correct-horse"}).status_code, 200)

    def test_revoke_and_ownership(self):
        t = self.make_transfer()
        other = {"X-Relay-Sender": "a" * 64}
        self.assertEqual(self.c.delete(f"/api/transfers/{t['id']}", headers=other).status_code, 404)
        tok = self.c.post("/api/receive", headers=self.h, json={"reference": t["code"]}).get_json()["download_token"]
        self.assertEqual(self.c.delete(f"/api/transfers/{t['id']}", headers=self.h).status_code, 200)
        self.assertEqual(self.c.get(f"/download?token={tok}").status_code, 401)  # grants are deleted on revoke
        self.assertEqual(self.c.post("/api/receive", headers=self.h, json={"reference": t["code"]}).status_code, 404)
        self.assertEqual(list((Path(self.tmp.name) / "files").iterdir()), [])

    def test_validation(self):
        bad = self.c.post("/api/transfers", headers=self.h, json={"files": [], "hours": 24})
        self.assertEqual(bad.status_code, 400)
        self.assertEqual(self.c.post("/api/transfers", headers=self.h, json={"files": [{"name": "a", "size": 1}], "hours": 5}).status_code, 400)
        self.assertEqual(self.c.post("/api/transfers", headers=self.h, json={"files": [{"name": "a", "size": 51 * 1024 * 1024}], "hours": 24}).status_code, 413)
        self.assertEqual(self.c.post("/api/transfers", headers=self.h, json={"files": [{"name": "a", "size": 1}], "hours": True}).status_code, 400)
        self.assertEqual(self.c.get("/api/session", headers={"X-Relay-Sender": "xyz"}).status_code, 400)
        self.assertEqual(self.c.post("/api/transfers", headers={**self.h, "Origin": "http://evil.example"}, json={}).status_code, 403)
        self.assertEqual(self.c.get("/api/nothing").status_code, 404)

    def test_upload_size_mismatch(self):
        r = self.c.post("/api/transfers", headers=self.h, json={"files": [{"name": "a.txt", "size": 10}], "hours": 1}).get_json()
        f = r["files"][0]
        bad = self.c.put(f"/api/transfers/{r['id']}/files/{f['id']}", headers=self.h, data=b"short")
        self.assertEqual(bad.status_code, 400)
        fin = self.c.post(f"/api/transfers/{r['id']}/finalize", headers=self.h)
        self.assertEqual(fin.status_code, 409)

    def test_tickets_and_admin(self):
        t = self.make_transfer()
        r = self.c.post("/api/tickets", headers=self.h, json={"kind": "abuse", "name": "N", "email": "a@b.co", "subject": "S",
                                                            "message": "x" * 25, "reference": t["code"]})
        self.assertEqual(r.status_code, 201)
        ticket = r.get_json()
        st = self.c.get(f"/api/tickets/{ticket['id']}?receipt={ticket['receipt']}", headers=self.h)
        self.assertEqual(st.get_json()["status"], "open")
        self.assertEqual(self.c.get("/api/admin/tickets", headers=self.h).status_code, 401)
        self.assertEqual(self.c.post("/api/admin/login", headers=self.h, json={"key": "nope"}).status_code, 401)
        self.assertEqual(self.c.post("/api/admin/login", headers=self.h, json={"key": "secret-key"}).status_code, 200)
        lst = self.c.get("/api/admin/tickets", headers=self.h).get_json()["tickets"]
        self.assertEqual(lst[0]["id"], ticket["id"])
        up = self.c.patch(f"/api/admin/tickets/{ticket['id']}", headers=self.h, json={"status": "resolved", "response": "Done", "revoke": True})
        self.assertEqual(up.status_code, 200)
        st = self.c.get(f"/api/tickets/{ticket['id']}?receipt={ticket['receipt']}", headers=self.h).get_json()
        self.assertEqual((st["status"], st["response"]), ("resolved", "Done"))
        self.assertEqual(self.c.post("/api/receive", headers=self.h, json={"reference": t["code"]}).status_code, 404)
        short = self.c.post("/api/tickets", headers=self.h, json={"kind": "contact", "name": "N", "email": "a@b.co", "subject": "S", "message": "short"})
        self.assertEqual(short.status_code, 400)

    def test_qr_and_barcode_endpoints(self):
        r = self.c.post("/api/qr", headers=self.h, json={"text": "http://localhost/?receive=" + "a" * 64})
        self.assertTrue(r.data.startswith(b"\x89PNG"))
        self.assertTrue(self.c.post("/api/qr", headers=self.h, json={"text": "hi", "format": "svg"}).data.startswith(b"<svg"))
        self.assertTrue(self.c.post("/api/barcode", headers=self.h, json={"code": "01234567"}).data.startswith(b"\x89PNG"))
        self.assertEqual(self.c.post("/api/barcode", headers=self.h, json={"code": "é"}).status_code, 400)

    def test_safe_names(self):
        self.assertEqual(safe_names(["../../etc/passwd", "a.txt", "A.txt", "con.txt", "  ..hidden. ", "", "x<y>.txt"]),
                         ["passwd", "a.txt", "A (2).txt", "_con.txt", "hidden", "file-6", "x_y_.txt"])


if __name__ == "__main__":
    unittest.main()
