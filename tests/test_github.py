"""GitHub 연동 경로 검증. 실제 GitHub 대신 같은 API 모양의 가짜 서버를 띄워 확인한다."""
import base64
import json
import os
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

CID, SECRET, TOKEN = "Iv1.test", "test-secret", "gho_testtoken123"


class FakeGitHub:
    def __init__(self):
        self.active = {TOKEN}
        self.revoke_calls = 0
        fake = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, code, body=None):
                raw = json.dumps(body).encode() if body is not None else b""
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def _body(self):
                n = int(self.headers.get("Content-Length") or 0)
                return json.loads(self.rfile.read(n) or b"{}")

            def _basic_ok(self):
                want = "Basic " + base64.b64encode(f"{CID}:{SECRET}".encode()).decode()
                return self.headers.get("Authorization") == want

            def do_GET(self):
                if self.headers.get("Authorization", "")[7:] not in fake.active:
                    return self._send(401, {"message": "Bad credentials"})
                if self.path.startswith("/user/repos"):
                    return self._send(200, [{"full_name": "sohyun/connector-guard"}])
                if self.path == "/user":
                    return self._send(200, {"login": "sohyun", "public_repos": 3})
                self._send(404, {})

            def do_POST(self):  # 토큰 확인
                if self.path != f"/applications/{CID}/token" or not self._basic_ok():
                    return self._send(404, {})
                tok = self._body().get("access_token")
                return self._send(200, {"token": tok}) if tok in fake.active else self._send(404, {})

            def do_DELETE(self):  # 토큰 폐기
                if self.path != f"/applications/{CID}/token" or not self._basic_ok():
                    return self._send(422, {})
                fake.revoke_calls += 1
                fake.active.discard(self._body().get("access_token"))
                self._send(204)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"


class GitHubFlowTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fake = FakeGitHub()
        cls.tmp = tempfile.mkdtemp()
        token_file = os.path.join(cls.tmp, "token.json")
        with open(token_file, "w") as f:
            json.dump({"access_token": TOKEN}, f)
        os.environ.update(GITHUB_API=cls.fake.url, GITHUB_CLIENT_ID=CID, GITHUB_CLIENT_SECRET=SECRET,
                          GITHUB_TOKEN_FILE=token_file)
        cls.token_file = token_file
        import demo_github
        from demo import Demo
        cls.demo = Demo(quiet=True, connectors=demo_github.CONNECTOR)
        cls.out = demo_github.run(cls.demo)

    @classmethod
    def tearDownClass(cls):
        cls.demo.close()
        cls.fake.server.shutdown()

    def test_low_risk_minimal_scope(self):
        self.assertEqual(self.out["risk"], "low")  # read:user → 저위험(180일)

    def test_real_calls_and_pause(self):
        self.assertTrue(self.out["first_call_ok"])
        self.assertEqual(self.out["paused"], "paused")
        self.assertTrue(self.out["retry_ok"])

    def test_revoked_at_github(self):
        self.assertEqual(self.out["state"], "released")
        self.assertEqual(self.out["github_status"], 204)
        self.assertTrue(self.out["revoked"])
        self.assertFalse(self.out["active_after"])
        self.assertEqual(self.fake.revoke_calls, 1)
        self.assertNotIn(TOKEN, self.fake.active)
        self.assertTrue(self.out["after_blocked"])
        with open(self.token_file) as f:
            self.assertEqual(json.load(f), {})  # 로그인 사본도 파기


if __name__ == "__main__":
    unittest.main()
