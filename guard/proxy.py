"""감시 프록시(제안서 3.5의 핵심).

AI 에이전트 쪽에는 MCP 서버로, 아래쪽 MCP 서버들에는 MCP 클라이언트로 동작한다.
모든 도구 호출(tools/call)이 이곳을 지나므로 여기서
  · 호출 출처별 처리와 사용 이력 기록
  · 위험 등급별 미사용 판정과 상태 전환(활성 → 사전 고지 → 일시정지 → 해제)
  · 이용자 본인만 할 수 있는 재확인
  · 해제 시 MCP 서버·발급기관으로 이어지는 연쇄 폐기
를 수행한다.

호출 출처는 에이전트가 요청의 _meta.callSource 에 표시해 보낸다고 가정한다
(user / scheduled / autonomous). 표시가 없으면 가장 보수적으로 'autonomous'로 본다.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import secrets
import sys

from . import policy
from .jsonrpc import PROTOCOL_VERSION, RpcError, StdioClient, serve_stdio, text_result
from .store import Store

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SOURCES = ("user", "scheduled", "autonomous")
SOURCE_LABEL = {"user": "이용자 요청", "scheduled": "등록된 예약 작업", "autonomous": "에이전트 자율"}

# 시연용 커넥터 두 개
DEFAULT_CONNECTORS = [
    {"id": "notes", "name": "메모 서비스", "resource": "notes",
     "scopes": ["notes.read", "notes.write"], "schedule_days": None},
    {"id": "crm", "name": "고객관리(CRM)", "resource": "contacts",
     "scopes": ["crm.all"], "schedule_days": 120},   # 'crm.all'은 분류표에 없는 권한 → 판별 불가
]


class Downstream:
    """하위 MCP 서버 하나(자격증명을 보관하는 커넥터)."""

    def __init__(self, cfg: dict, agent_id: str, workdir: str, issuer_url: str):
        env = dict(os.environ, ISSUER_URL=issuer_url, RESOURCE=cfg["resource"],
                   VAULT_PATH=os.path.join(workdir, f"vault_{cfg['id']}.json"),
                   PYTHONPATH=ROOT, PYTHONIOENCODING="utf-8")
        module = "guard.github_server" if cfg.get("provider") == "github" else "guard.resource_server"
        self.client = StdioClient([sys.executable, "-m", module], env=env, cwd=ROOT)
        self.client.initialize(agent_id)
        self.tools = self.client.request("tools/list")["tools"]

    def close(self):
        self.client.close()


class Guard:
    def __init__(self, store: Store, agent_id: str = "agent-1"):
        self.s = store
        self.lock = store.lock
        self.agent_id = agent_id
        self.down: dict[str, Downstream] = {}

    # ------------------------------------------------------------------ 등록
    def attach(self, cfg: dict, downstream: Downstream) -> None:
        self.down[cfg["id"]] = downstream
        with self.lock:
            if self.s.one("SELECT 1 FROM connectors WHERE id = ?", cfg["id"]):
                return
            risk, reason = policy.risk_of(cfg["scopes"])
            config = {"resource": cfg["resource"], "schedule_days": cfg["schedule_days"],
                      "tools": [t["name"] for t in downstream.tools]}
            today = self.s.today().isoformat()
            self.s.exec("INSERT INTO connectors VALUES (?,?,?,?,?,?)", cfg["id"], cfg["name"],
                        json.dumps(cfg["scopes"]), risk, cfg["schedule_days"],
                        json.dumps(config, ensure_ascii=False))
            self.s.exec("INSERT INTO cred_state (connector_id, state, last_activity, last_reconfirm) "
                        "VALUES (?, 'active', ?, ?)", cfg["id"], today, today)
            self.s.event(cfg["id"], "registered", "system",
                         {"risk": risk, "reason": reason, "scopes": cfg["scopes"],
                          "threshold_days": policy.inactivity_threshold(risk, cfg["schedule_days"])})

    def _row(self, cid: str):
        return self.s.one("SELECT c.*, s.* FROM connectors c JOIN cred_state s ON s.connector_id = c.id "
                          "WHERE c.id = ?", cid)

    def rows(self):
        return self.s.all("SELECT c.*, s.* FROM connectors c JOIN cred_state s ON s.connector_id = c.id "
                          "ORDER BY c.id")

    # ------------------------------------------------------------------ 도구 목록
    def list_tools(self) -> list[dict]:
        tools = []
        for r in self.rows():
            if r["state"] == "released":
                continue  # 해제된 커넥터의 도구는 더 이상 보이지 않는다
            for t in self.down[r["id"]].tools:
                tools.append({**t, "name": f"{r['id']}__{t['name']}"})
        return tools

    # ------------------------------------------------------------------ 호출 처리
    def _record(self, cid, tool, source, outcome, counted):
        # 도구 인자(이용자의 요청 내용)는 저장하지 않는다
        self.s.exec("INSERT INTO calls (day, connector_id, tool, source, outcome, counted) "
                    "VALUES (?,?,?,?,?,?)", self.s.today().isoformat(), cid, tool, source, outcome,
                    int(counted))

    def call(self, name: str, args: dict, meta: dict) -> dict:
        source = meta.get("callSource")
        if source not in SOURCES:
            source = "autonomous"

        # 에이전트가 재확인을 대신하려는 시도: 재확인은 도구로 노출하지 않으며 항상 거부한다
        if name.startswith("guard__") or "reconfirm" in name:
            with self.lock:
                self.s.event(args.get("connector"), "reconfirm_denied", "agent",
                             {"tool": name, "source": source})
            return text_result("재확인은 플랫폼 화면에서 이용자 본인만 할 수 있습니다. "
                               "에이전트는 재확인을 대신할 수 없습니다.", is_error=True)

        cid, _, tool = name.partition("__")
        with self.lock:
            r = self._row(cid)
            if r is None:
                raise RpcError(-32602, f"unknown tool: {name}")
            state = r["state"]

            if state == "released":
                self._record(cid, tool, source, "blocked_released", False)
                return text_result("연결이 해제된 커넥터입니다. 다시 연결하려면 새로 동의해야 합니다.",
                                   is_error=True)

            if state == "paused":
                self._record(cid, tool, source, "blocked", False)  # 차단된 호출은 활동으로 세지 않는다
                return self._handle_paused(r, tool, source)

        # 활성·사전 고지 상태: 통과
        result = self.down[cid].client.request("tools/call", {"name": tool, "arguments": args})
        counted = source == "user" or (source == "scheduled" and bool(r["schedule_days"]))
        with self.lock:
            self._record(cid, tool, source, "passed", counted)
            if counted:
                self.s.exec("UPDATE cred_state SET last_activity = ? WHERE connector_id = ?",
                            self.s.today().isoformat(), cid)
                if r["state"] == "notified" and r["notice_reason"] == "inactive":
                    # 사전 고지 기간에 사용하면 활성으로 복귀
                    self.s.exec("UPDATE cred_state SET state='active', notice_at=NULL, notice_reason=NULL "
                                "WHERE connector_id = ?", cid)
                    self.s.event(cid, "reactivated", "user", "사전 고지 기간 중 사용")
        return result

    def _handle_paused(self, r, tool: str, source: str) -> dict:
        cid = r["id"]
        if source == "user":
            desc = next((t["description"] for t in self.down[cid].tools if t["name"] == tool), tool)
            label = f"‘{desc.rstrip('.')}’ 요청에 이 커넥터가 필요합니다"
            self.issue_nonce(cid, label)
            self.s.notify(cid, "inline-card", "reconfirm_card",
                          f"[재확인 카드] {r['name']} · {label} · [계속 사용] [연결 해제]")
            self.s.event(cid, "card_shown", "system", {"tool": tool})
            # 에이전트에게는 재확인 수단(nonce)을 주지 않는다. 카드는 이용자 화면에만 뜬다.
            return text_result(f"'{r['name']}' 커넥터가 장기간 사용되지 않아 일시정지되었습니다. "
                               "이용자 화면에 재확인 카드를 표시했습니다. 이용자가 [계속 사용]을 "
                               "선택하면 요청을 다시 실행하세요.", is_error=True,
                               guard={"state": "paused", "card": "shown"})
        if source == "scheduled":
            self.s.event(cid, "scheduled_skipped", "system", {"tool": tool})
            if not r["sched_alerted"]:
                self.s.notify(cid, "email+push", "scheduled_skipped",
                              f"{r['name']} 커넥터가 정지되어 예약 작업 실행을 건너뛰었습니다. "
                              "계속 사용하려면 대시보드에서 재확인하세요.")
                self.s.exec("UPDATE cred_state SET sched_alerted = 1 WHERE connector_id = ?", cid)
            return text_result("커넥터가 일시정지되어 예약 작업 실행을 건너뛰었습니다.", is_error=True,
                               guard={"state": "paused", "skipped": True})
        # 에이전트 자율 호출: 차단만 하고 알리지 않는다(승인 유도 수단으로 쓰이지 않게)
        self.s.event(cid, "autonomous_blocked", "system", {"tool": tool})
        return text_result("일시정지된 커넥터입니다.", is_error=True, guard={"state": "paused"})

    # ------------------------------------------------------------------ 재확인
    def issue_nonce(self, cid: str, label: str | None = None) -> str:
        nonce = secrets.token_urlsafe(16)
        self.s.exec("INSERT INTO nonces (nonce, connector_id, day, request_label) VALUES (?,?,?,?)",
                    nonce, cid, self.s.today().isoformat(), label)
        return nonce

    def decide(self, nonce: str, decision: str, actor: str = "user") -> tuple[bool, str]:
        """재확인 화면의 버튼 처리. 이용자 화면에서 발급된 1회용 값이 있어야만 유효하다."""
        with self.lock:
            n = self.s.one("SELECT * FROM nonces WHERE nonce = ?", nonce or "")
            if n is None or n["used"]:
                self.s.event(n["connector_id"] if n else None, "reconfirm_denied", "unverified",
                             "유효하지 않은 재확인 요청(1회용 값 없음·재사용)")
                return False, "유효하지 않은 재확인 요청입니다."
            cid = n["connector_id"]
            r = self._row(cid)
            self.s.exec("UPDATE nonces SET used = 1 WHERE connector_id = ?", cid)
            if r["state"] == "released":
                return False, "이미 해제된 커넥터입니다."
            if decision == "keep":
                today = self.s.today().isoformat()
                self.s.exec("UPDATE cred_state SET state='active', last_activity=?, last_reconfirm=?, "
                            "notice_at=NULL, notice_reason=NULL, paused_at=NULL, pause_reason=NULL, "
                            "sched_alerted=0, final_notice_at=NULL WHERE connector_id=?", today, today, cid)
                self.s.event(cid, "reconfirmed", "user", "계속 사용")
                return True, f"{r['name']}: 다시 사용할 수 있습니다(기존 설정 유지)."
        if decision == "disconnect":
            self.release(cid, "이용자가 연결 해제 선택", actor="user")
            return True, f"{r['name']}: 연결을 해제하고 토큰을 폐기했습니다."
        return False, "알 수 없는 선택입니다."

    # ------------------------------------------------------------------ 판정(1일 1회)
    def tick(self) -> None:
        today = self.s.today()
        for r in self.rows():
            cid, st = r["id"], r["state"]
            if st in ("active", "notified"):
                thr = policy.inactivity_threshold(r["risk"], r["schedule_days"])
                idle = (today - dt.date.fromisoformat(r["last_activity"])).days
                since = (today - dt.date.fromisoformat(r["last_reconfirm"])).days
                if idle >= thr or since >= policy.ABSOLUTE_CAP_DAYS:
                    reason = "inactive" if idle >= thr else "absolute_cap"
                    self._pause(r, reason, idle, since)
                elif r["notice_at"] is None:
                    if idle >= thr - policy.NOTICE_BEFORE_DAYS:
                        self._notice(r, "inactive", f"{idle}일간 사용되지 않아 "
                                     f"{thr - idle}일 뒤 일시정지됩니다.")
                    elif since >= policy.ABSOLUTE_CAP_DAYS - policy.NOTICE_BEFORE_DAYS:
                        self._notice(r, "absolute_cap", "마지막 확인 후 12개월이 곧 지나 "
                                     f"{policy.ABSOLUTE_CAP_DAYS - since}일 뒤 재확인이 필요합니다.")
            elif st == "paused":
                paused = (today - dt.date.fromisoformat(r["paused_at"])).days
                if paused >= policy.RELEASE_AFTER_PAUSE_DAYS:
                    self.release(cid, "일시정지 후 추가 무응답", actor="system")
                elif (paused >= policy.RELEASE_AFTER_PAUSE_DAYS - policy.FINAL_NOTICE_BEFORE_DAYS
                      and r["final_notice_at"] is None):
                    self.s.notify(cid, "email", "final_notice",
                                  f"{r['name']}: {policy.RELEASE_AFTER_PAUSE_DAYS - paused}일 뒤 연결이 "
                                  "해제되고 구성 정보가 삭제됩니다.")
                    self.s.exec("UPDATE cred_state SET final_notice_at = ? WHERE connector_id = ?",
                                today.isoformat(), cid)
                    self.s.event(cid, "final_notice", "system", "")

    def _notice(self, r, reason: str, text: str) -> None:
        self.s.notify(r["id"], "email+push", "pre_notice", f"{r['name']}: {text}")
        self.s.exec("UPDATE cred_state SET state='notified', notice_at=?, notice_reason=? "
                    "WHERE connector_id=?", self.s.today().isoformat(), reason, r["id"])
        self.s.event(r["id"], "pre_notice", "system", {"reason": reason})

    def _pause(self, r, reason: str, idle: int, since: int) -> None:
        self.s.exec("UPDATE cred_state SET state='paused', paused_at=?, pause_reason=?, sched_alerted=0, "
                    "final_notice_at=NULL WHERE connector_id=?", self.s.today().isoformat(), reason, r["id"])
        self.s.event(r["id"], "paused", "system",
                     {"reason": reason, "idle_days": idle, "days_since_reconfirm": since,
                      "config_preserved": True})

    # ------------------------------------------------------------------ 해제·연쇄 폐기
    def release(self, cid: str, why: str, actor: str) -> dict:
        with self.lock:
            r = self._row(cid)
            if r["state"] == "released":
                return {}
            self.s.exec("UPDATE cred_state SET state='released' WHERE connector_id=?", cid)
            self.s.exec("UPDATE connectors SET config = NULL WHERE id = ?", cid)  # 구성 정보 삭제
            self.s.event(cid, "released", actor, why)
        result, err = {}, None
        for _ in range(3):  # MCP 서버에 폐기 통지(실패 시 재시도)
            try:
                result = self.down[cid].client.request("x-guard/revoke", {"agent_id": self.agent_id})
                break
            except Exception as e:  # noqa: BLE001
                err = repr(e)
        with self.lock:
            if result:
                self.s.event(cid, "revoke_cascade", "system", result)
            else:
                self.s.event(cid, "revoke_failed", "system", err or "")
                self.s.notify(cid, "email", "manual_revoke",
                              f"{r['name']}: 자동 폐기에 실패했습니다. 서비스 설정에서 직접 연결을 해제해 주세요.")
        return result

    # ------------------------------------------------------------------ 가상 시계
    def advance(self, days: int) -> None:
        for _ in range(days):
            with self.lock:
                self.s.set_today(self.s.today() + dt.timedelta(days=1))
            self.tick()


# ====================================================================== 실행부
def main() -> None:
    workdir = os.environ.get("GUARD_WORKDIR", os.path.join(ROOT, "run"))
    os.makedirs(workdir, exist_ok=True)
    store = Store(os.environ.get("GUARD_DB", os.path.join(workdir, "guard.db")))
    guard = Guard(store, os.environ.get("AGENT_ID", "agent-1"))
    issuer_url = os.environ.get("ISSUER_URL", "http://127.0.0.1:9000")
    ui_port = int(os.environ.get("GUARD_UI_PORT", "8765"))
    started = {"done": False}

    def start():
        if started["done"]:
            return
        connectors = json.loads(os.environ["GUARD_CONNECTORS"]) if os.environ.get("GUARD_CONNECTORS") \
            else DEFAULT_CONNECTORS
        for cfg in connectors:
            guard.attach(cfg, Downstream(cfg, guard.agent_id, workdir, issuer_url))
        from .ui import start_ui
        start_ui(guard, ui_port)
        print(f"[guard] 재확인 화면: http://127.0.0.1:{ui_port}/", file=sys.stderr)
        started["done"] = True

    def handle(method: str, params: dict):
        if method == "initialize":
            start()
            return {"protocolVersion": PROTOCOL_VERSION, "capabilities": {"tools": {}},
                    "serverInfo": {"name": "connector-guard-proxy", "version": "0.1"}}
        if method.startswith("notifications/"):
            return None
        if method == "tools/list":
            return {"tools": guard.list_tools()}
        if method == "tools/call":
            return guard.call(params["name"], params.get("arguments") or {}, params.get("_meta") or {})
        if method == "x-guard/reconfirm":  # 에이전트가 프로토콜로 재확인을 시도하는 경우
            guard.s.event(params.get("connector"), "reconfirm_denied", "agent", "프로토콜 메서드 시도")
            raise RpcError(-32001, "재확인은 이용자 본인만 할 수 있습니다.")
        # --- 시연용 관리 메서드(가상 시계와 조회) ---
        if method == "x-admin/advance":
            guard.advance(int(params.get("days", 1)))
            return {"day": store.day_no(), "date": store.today().isoformat()}
        if method == "x-admin/dump":
            since = int(params.get("since_event", 0))
            since_n = int(params.get("since_notice", 0))
            return {
                "day": store.day_no(), "date": store.today().isoformat(),
                "connectors": [dict(r) for r in guard.rows()],
                "events": [dict(e) for e in store.all("SELECT * FROM events WHERE id > ?", since)],
                "notifications": [dict(n) for n in store.all("SELECT * FROM notifications WHERE id > ?",
                                                             since_n)],
            }
        raise RpcError(-32601, f"method not found: {method}")

    try:
        serve_stdio(handle)
    finally:
        for d in guard.down.values():
            d.close()


if __name__ == "__main__":
    main()
