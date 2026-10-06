"""가상 발급기관(외부 서비스 역할).

실제 세계의 노션·구글 같은 서비스를 흉내 낸다.
- POST /token       : 이용자 동의를 받은 것으로 보고 갱신 토큰·액세스 토큰 발급 (시연용 간소화)
- POST /revoke      : RFC 7009 토큰 폐기
- POST /introspect  : RFC 7662 토큰 유효성 조회
- GET/POST /api/<resource> : 액세스 토큰으로 접근하는 데이터 API
"""
from __future__ import annotations

import base64
import json
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

CLIENT_ID = "demo-client"
CLIENT_SECRET = "demo-secret"  # 시연용 값. 실제 서비스의 비밀값이 아니다.


class IssuerState:
    def __init__(self):
        self.lock = threading.Lock()
        self.refresh: dict[str, dict] = {}   # refresh_token -> {resource, active, access}
        self.access: dict[str, str] = {}     # access_token -> refresh_token
        self.data: dict[str, list] = {
            "notes": [{"id": 1, "title": "주간 회의록"}, {"id": 2, "title": "공모전 일정"}],
            "contacts": [{"id": 1, "name": "고객사 A 담당자"}],
        }

    def issue(self, resource: str) -> dict:
        with self.lock:
            rt, at = "rt_" + secrets.token_urlsafe(16), "at_" + secrets.token_urlsafe(16)
            self.refresh[rt] = {"resource": resource, "active": True, "access": {at}}
            self.access[at] = rt
            return {"refresh_token": rt, "access_token": at, "token_type": "Bearer"}

    def revoke(self, token: str) -> None:
        # RFC 7009 §2.1: 갱신 토큰을 폐기하면 같은 승인에서 나온 액세스 토큰도 폐기할 수 있다.
        with self.lock:
            rt = token if token in self.refresh else self.access.get(token)
            if rt and rt in self.refresh:
                self.refresh[rt]["active"] = False

    def is_active(self, token: str) -> tuple[bool, str | None]:
        with self.lock:
            rt = token if token in self.refresh else self.access.get(token)
            if not rt or rt not in self.refresh:
                return False, None
            info = self.refresh[rt]
            return info["active"], info["resource"]


def _make_handler(state: IssuerState):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):  # 콘솔을 조용히 유지
            pass

        def _json(self, code: int, body: dict) -> None:
            raw = json.dumps(body, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def _form(self) -> dict:
            n = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(n).decode("utf-8")
            if self.headers.get("Content-Type", "").startswith("application/json"):
                return json.loads(raw or "{}")
            return {k: v[0] for k, v in parse_qs(raw).items()}

        def _client_ok(self) -> bool:
            auth = self.headers.get("Authorization", "")
            if not auth.startswith("Basic "):
                return False
            cid, _, sec = base64.b64decode(auth[6:]).decode().partition(":")
            return cid == CLIENT_ID and sec == CLIENT_SECRET

        def _bearer_resource(self) -> str | None:
            auth = self.headers.get("Authorization", "")
            if not auth.startswith("Bearer "):
                return None
            active, resource = state.is_active(auth[7:])
            return resource if active else None

        def do_POST(self):
            path = urlparse(self.path).path
            if path in ("/token", "/revoke", "/introspect") and not self._client_ok():
                return self._json(401, {"error": "invalid_client"})
            if path == "/token":
                form = self._form()
                return self._json(200, state.issue(form.get("resource", "notes")))
            if path == "/revoke":
                state.revoke(self._form().get("token", ""))
                return self._json(200, {})  # RFC 7009: 알 수 없는 토큰이어도 200
            if path == "/introspect":
                active, resource = state.is_active(self._form().get("token", ""))
                return self._json(200, {"active": active, **({"scope": resource} if active else {})})
            if path.startswith("/api/"):
                resource = path.split("/")[2]
                if self._bearer_resource() != resource:
                    return self._json(401, {"error": "invalid_token"})
                item = self._form()
                with state.lock:
                    items = state.data.setdefault(resource, [])
                    item["id"] = len(items) + 1
                    items.append(item)
                return self._json(201, item)
            return self._json(404, {"error": "not_found"})

        def do_GET(self):
            path = urlparse(self.path).path
            if path.startswith("/api/"):
                resource = path.split("/")[2]
                if self._bearer_resource() != resource:
                    return self._json(401, {"error": "invalid_token"})
                with state.lock:
                    return self._json(200, {"items": list(state.data.get(resource, []))})
            return self._json(404, {"error": "not_found"})

    return Handler


def start_issuer(port: int = 0) -> tuple[ThreadingHTTPServer, IssuerState]:
    """발급기관을 백그라운드 스레드로 띄우고 (서버, 상태)를 돌려준다."""
    state = IssuerState()
    server = ThreadingHTTPServer(("127.0.0.1", port), _make_handler(state))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, state


if __name__ == "__main__":
    srv, _ = start_issuer(9000)
    print("가상 발급기관: http://127.0.0.1:9000")
    threading.Event().wait()
