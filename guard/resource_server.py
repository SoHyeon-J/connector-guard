"""로컬 MCP 서버(커넥터 역할).

노션 MCP 서버처럼 외부 서비스의 토큰을 '직접 보관'하고 도구를 제공한다.
감시 프록시에서 연결을 끊어도 이 서버의 보관본은 남기 때문에,
해제 시에는 프록시가 이 서버에 폐기를 통지해야 한다(연쇄 폐기).

환경 변수
  ISSUER_URL   가상 발급기관 주소
  RESOURCE     제공할 자원 이름 (notes, contacts)
  VAULT_PATH   토큰 보관 파일 경로
"""
from __future__ import annotations

import base64
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

from .issuer import CLIENT_ID, CLIENT_SECRET
from .jsonrpc import PROTOCOL_VERSION, RpcError, serve_stdio, text_result

ISSUER = os.environ.get("ISSUER_URL", "http://127.0.0.1:9000")
RESOURCE = os.environ.get("RESOURCE", "notes")
VAULT = os.environ.get("VAULT_PATH", f"vault_{RESOURCE}.json")
LABEL = {"notes": "메모", "contacts": "연락처"}.get(RESOURCE, RESOURCE)


# ---------- HTTP 도우미 ----------
def _call(method: str, path: str, *, form: dict | None = None, bearer: str | None = None,
          basic: bool = False) -> tuple[int, dict]:
    data = urllib.parse.urlencode(form).encode() if form is not None else None
    req = urllib.request.Request(ISSUER + path, data=data, method=method)
    if form is not None:
        req.add_header("Content-Type", "application/x-www-form-urlencoded")
    if bearer:
        req.add_header("Authorization", "Bearer " + bearer)
    if basic:
        cred = base64.b64encode(f"{CLIENT_ID}:{CLIENT_SECRET}".encode()).decode()
        req.add_header("Authorization", "Basic " + cred)
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


# ---------- 토큰 보관소 ----------
def load_vault() -> dict:
    try:
        with open(VAULT, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {}


def save_vault(v: dict) -> None:
    with open(VAULT, "w", encoding="utf-8") as f:
        json.dump(v, f, ensure_ascii=False)


def ensure_token(agent_id: str) -> dict:
    """처음 연결될 때 이용자 동의를 받아 토큰을 발급받은 것으로 본다(시연용 간소화)."""
    v = load_vault()
    if not v.get("refresh_token"):
        _, tok = _call("POST", "/token", form={"resource": RESOURCE}, basic=True)
        v = {"refresh_token": tok["refresh_token"], "access_token": tok["access_token"], "links": []}
    if agent_id not in v["links"]:
        v["links"].append(agent_id)  # 이 자격증명을 쓰는 상위 연결(참조 카운트)
    save_vault(v)
    return v


# ---------- MCP 도구 ----------
TOOLS = [
    {"name": f"list_{RESOURCE}", "description": f"{LABEL} 목록을 조회한다.",
     "inputSchema": {"type": "object", "properties": {}},
     "annotations": {"readOnlyHint": True}},
    {"name": f"create_{RESOURCE}", "description": f"{LABEL}를 새로 만든다.",
     "inputSchema": {"type": "object", "properties": {"title": {"type": "string"}}},
     "annotations": {"readOnlyHint": False}},
]


def call_tool(name: str, args: dict) -> dict:
    v = load_vault()
    if not v.get("access_token"):
        return text_result("보관된 자격증명이 없습니다.", is_error=True)
    if name == f"list_{RESOURCE}":
        code, body = _call("GET", f"/api/{RESOURCE}", bearer=v["access_token"])
    elif name == f"create_{RESOURCE}":
        code, body = _call("POST", f"/api/{RESOURCE}", form=args, bearer=v["access_token"])
    else:
        raise RpcError(-32602, f"unknown tool: {name}")
    if code >= 400:
        return text_result(f"외부 서비스 오류 {code}: {body}", is_error=True)
    return text_result(json.dumps(body, ensure_ascii=False))


def revoke(agent_id: str, retries: int = 3) -> dict:
    """폐기 통지 처리: 참조가 모두 사라지면 보관본 파기 후 발급기관에 폐기 요청."""
    v = load_vault()
    if not v.get("refresh_token"):
        return {"revoked": True, "note": "이미 보관본 없음"}
    links = [x for x in v.get("links", []) if x != agent_id]
    if links:  # 다른 에이전트가 아직 같은 자격증명을 쓰는 중
        v["links"] = links
        save_vault(v)
        return {"revoked": False, "remaining_links": len(links)}
    token = v["refresh_token"]
    ok = False
    for i in range(retries):  # 실패 시 재시도
        code, _ = _call("POST", "/revoke", form={"token": token, "token_type_hint": "refresh_token"},
                        basic=True)
        if code == 200:
            ok = True
            break
        time.sleep(0.2 * (i + 1))
    save_vault({})  # 보관본 파기
    _, intro = _call("POST", "/introspect", form={"token": token}, basic=True)
    return {"revoked": ok, "vault_cleared": True, "introspect_active": intro.get("active"),
            "token_hint": token[:6] + "…"}


def handle(method: str, params: dict):
    if method == "initialize":
        ensure_token(params.get("clientInfo", {}).get("name", "agent"))
        return {"protocolVersion": PROTOCOL_VERSION, "capabilities": {"tools": {}},
                "serverInfo": {"name": f"{RESOURCE}-mcp-server", "version": "0.1"}}
    if method.startswith("notifications/"):
        return None
    if method == "tools/list":
        return {"tools": TOOLS}
    if method == "tools/call":
        return call_tool(params["name"], params.get("arguments") or {})
    if method == "x-guard/revoke":  # 프록시가 보내는 폐기 통지(프로토타입 확장 메서드)
        return revoke(params.get("agent_id", "agent"))
    raise RpcError(-32601, f"method not found: {method}")


if __name__ == "__main__":
    serve_stdio(handle)
