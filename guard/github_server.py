"""GitHub 커넥터 MCP 서버(선택 기능).

resource_server.py와 같은 역할을 실제 GitHub에 대해 수행한다.
- github_login.py로 받은 토큰을 가져와 직접 보관한다.
- 도구: 내 프로필 조회, 내 저장소 목록 조회(읽기 전용)
- 폐기 통지를 받으면 GitHub에 토큰 폐기를 요청하고, 보관본을 파기한 뒤,
  GitHub의 토큰 확인 API로 무효가 되었는지 확인한다.

환경 변수: VAULT_PATH(보관 파일), GITHUB_CLIENT_ID, GITHUB_CLIENT_SECRET, (테스트용) GITHUB_API
"""
from __future__ import annotations

import json
import os
import time

from . import github as gh
from .jsonrpc import PROTOCOL_VERSION, RpcError, serve_stdio, text_result

gh.load_env()
VAULT = os.environ.get("VAULT_PATH", "vault_github.json")
TOKEN_FILE = os.environ.get("GITHUB_TOKEN_FILE", gh.TOKEN_FILE)


def load_vault() -> dict:
    try:
        with open(VAULT, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {}


def save_vault(v: dict) -> None:
    with open(VAULT, "w", encoding="utf-8") as f:
        json.dump(v, f)


def ensure_token(agent_id: str) -> None:
    """처음 연결될 때 github_login.py가 받아 둔 토큰을 가져와 보관한다."""
    v = load_vault()
    if not v.get("access_token"):
        try:
            with open(TOKEN_FILE, encoding="utf-8") as f:
                token = json.load(f).get("access_token")
        except FileNotFoundError:
            token = None
        if not token:
            return  # 토큰이 없으면 도구 호출 시 안내
        v = {"access_token": token, "links": []}
    if agent_id not in v["links"]:
        v["links"].append(agent_id)
    save_vault(v)


TOOLS = [
    {"name": "get_profile", "description": "내 GitHub 프로필을 조회한다.",
     "inputSchema": {"type": "object", "properties": {}}, "annotations": {"readOnlyHint": True}},
    {"name": "list_repos", "description": "내 GitHub 저장소 목록을 조회한다.",
     "inputSchema": {"type": "object", "properties": {}}, "annotations": {"readOnlyHint": True}},
]


def call_tool(name: str) -> dict:
    token = load_vault().get("access_token")
    if not token:
        return text_result("보관된 GitHub 토큰이 없습니다. 먼저 python github_login.py 를 실행하세요.",
                           is_error=True)
    if name == "get_profile":
        code, body = gh.get(token, "/user")
        summary = {"login": body.get("login"), "public_repos": body.get("public_repos")} if code == 200 else body
    elif name == "list_repos":
        code, body = gh.get(token, "/user/repos?per_page=5&sort=updated")
        summary = [r.get("full_name") for r in body] if code == 200 else body
    else:
        raise RpcError(-32602, f"unknown tool: {name}")
    if code != 200:
        return text_result(f"GitHub 오류 {code}: {summary}", is_error=True)
    return text_result(json.dumps(summary, ensure_ascii=False))


def revoke(agent_id: str, retries: int = 3) -> dict:
    v = load_vault()
    token = v.get("access_token")
    if not token:
        return {"revoked": True, "note": "이미 보관본 없음"}
    links = [x for x in v.get("links", []) if x != agent_id]
    if links:
        v["links"] = links
        save_vault(v)
        return {"revoked": False, "remaining_links": len(links)}
    code = None
    for i in range(retries):
        code = gh.revoke(token)
        if code == 204:
            break
        time.sleep(0.5 * (i + 1))
    save_vault({})                      # 보관본 파기
    if os.path.exists(TOKEN_FILE):      # 로그인 때 받은 사본도 파기
        with open(TOKEN_FILE, "w", encoding="utf-8") as f:
            json.dump({}, f)
    return {"revoked": code == 204, "github_status": code, "vault_cleared": True,
            "introspect_active": gh.is_active(token), "token_hint": token[:6] + "…"}


def handle(method: str, params: dict):
    if method == "initialize":
        ensure_token(params.get("clientInfo", {}).get("name", "agent"))
        return {"protocolVersion": PROTOCOL_VERSION, "capabilities": {"tools": {}},
                "serverInfo": {"name": "github-mcp-server", "version": "0.1"}}
    if method.startswith("notifications/"):
        return None
    if method == "tools/list":
        return {"tools": TOOLS}
    if method == "tools/call":
        return call_tool(params["name"])
    if method == "x-guard/revoke":
        return revoke(params.get("agent_id", "agent"))
    raise RpcError(-32601, f"method not found: {method}")


if __name__ == "__main__":
    serve_stdio(handle)
