"""GitHub OAuth App 연동 도우미(선택 기능).

- 기기 로그인(device flow)으로 이용자 동의를 받아 액세스 토큰 발급
- REST API 호출
- 토큰 폐기:  DELETE /applications/{client_id}/token  (성공 204)
- 유효 확인:  POST   /applications/{client_id}/token  (유효 200, 무효 404)

폐기·확인 API는 앱의 client_id와 client_secret(Basic 인증)이 필요하다.
설정은 프로젝트 폴더의 .env 파일에서 읽는다(저장소에 올리지 않음).
"""
from __future__ import annotations

import base64
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOKEN_FILE = os.path.join(ROOT, ".github_token.json")


def load_env(path: str = os.path.join(ROOT, ".env")) -> None:
    """KEY=VALUE 형식의 .env를 읽어 환경 변수로 넣는다(이미 있는 값은 유지)."""
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def api_base() -> str:
    return os.environ.get("GITHUB_API", "https://api.github.com")


def web_base() -> str:
    return os.environ.get("GITHUB_WEB", "https://github.com")


def client() -> tuple[str, str]:
    cid, sec = os.environ.get("GITHUB_CLIENT_ID", ""), os.environ.get("GITHUB_CLIENT_SECRET", "")
    if not cid or not sec:
        raise RuntimeError(".env에 GITHUB_CLIENT_ID와 GITHUB_CLIENT_SECRET을 넣어 주세요.")
    return cid, sec


def _request(method: str, url: str, *, body: dict | None = None, form: dict | None = None,
             bearer: str | None = None, basic: tuple[str, str] | None = None) -> tuple[int, dict | list]:
    data, headers = None, {"Accept": "application/vnd.github+json", "User-Agent": "connector-guard-poc",
                           "X-GitHub-Api-Version": "2022-11-28"}
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    if form is not None:
        data = urllib.parse.urlencode(form).encode()
        headers.update({"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"})
    if bearer:
        headers["Authorization"] = "Bearer " + bearer
    if basic:
        headers["Authorization"] = "Basic " + base64.b64encode(f"{basic[0]}:{basic[1]}".encode()).decode()
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            raw = r.read()
            return r.status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw) if raw else {}
        except ValueError:
            return e.code, {"raw": raw.decode(errors="replace")}


# ---------------- 기기 로그인 ----------------
def device_login(scope: str = "read:user", say=print) -> str:
    cid, _ = client()
    _, codes = _request("POST", web_base() + "/login/device/code", form={"client_id": cid, "scope": scope})
    if "device_code" not in codes:
        raise RuntimeError(f"기기 코드 발급 실패: {codes} (앱 설정에서 Device Flow를 켰는지 확인)")
    say(f"\n  1) 브라우저에서 {codes['verification_uri']} 를 여세요.")
    say(f"  2) 코드 {codes['user_code']} 를 입력하고 권한을 승인하세요. (요청 권한: {scope})\n")
    interval = int(codes.get("interval", 5))
    deadline = time.time() + int(codes.get("expires_in", 900))
    while time.time() < deadline:
        time.sleep(interval)
        _, tok = _request("POST", web_base() + "/login/oauth/access_token",
                          form={"client_id": cid, "device_code": codes["device_code"],
                                "grant_type": "urn:ietf:params:oauth:grant-type:device_code"})
        if "access_token" in tok:
            return tok["access_token"]
        err = tok.get("error")
        if err == "authorization_pending":
            continue
        if err == "slow_down":
            interval += 5
            continue
        raise RuntimeError(f"로그인 실패: {err}")
    raise RuntimeError("코드가 만료되었습니다. 다시 실행해 주세요.")


# ---------------- API ----------------
def get(token: str, path: str):
    return _request("GET", api_base() + path, bearer=token)


def revoke(token: str) -> int:
    cid, sec = client()
    code, _ = _request("DELETE", f"{api_base()}/applications/{cid}/token",
                       body={"access_token": token}, basic=(cid, sec))
    return code  # 204면 성공


def is_active(token: str) -> bool:
    cid, sec = client()
    code, _ = _request("POST", f"{api_base()}/applications/{cid}/token",
                       body={"access_token": token}, basic=(cid, sec))
    return code == 200  # 무효 토큰은 404
