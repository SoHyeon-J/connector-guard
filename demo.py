"""제안서 [표 3-5]의 6개 장면을 순서대로 재현한다.

    python demo.py            # 시연 실행
    python demo.py --interactive  # 장면 3에서 이용자가 브라우저로 직접 [계속 사용]을 누른다
    python demo.py --keep     # 끝난 뒤 재확인 화면을 계속 띄워 둔다(브라우저로 확인)

외부 패키지나 인터넷 연결 없이 동작한다. 가상 시계로 1년 반을 몇 초로 축약한다.
"""
from __future__ import annotations

import json
import os
import re
import socket
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

from guard.issuer import start_issuer  # noqa: E402
from guard.jsonrpc import StdioClient  # noqa: E402

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:  # noqa: BLE001
    pass

EVENT_TEXT = {
    "registered": "등록",
    "pre_notice": "사전 고지 발송",
    "paused": "일시정지(구성 정보 보존)",
    "card_shown": "이용자 화면에 재확인 카드 표시",
    "reconfirmed": "이용자 본인 재확인 → 활성 복귀",
    "reactivated": "사전 고지 중 사용 → 활성 복귀",
    "scheduled_skipped": "예약 작업 실행 건너뜀",
    "autonomous_blocked": "에이전트 자율 호출 차단(알림 없음)",
    "reconfirm_denied": "재확인 시도 거부",
    "final_notice": "최종 고지 발송",
    "released": "연결 해제 · 구성 정보 삭제",
    "revoke_cascade": "연쇄 폐기 결과",
    "revoke_failed": "연쇄 폐기 실패",
}


def free_port(preferred: int = 0) -> int:
    """preferred 포트가 비어 있으면 그 포트를, 아니면 빈 포트를 고른다."""
    for port in (preferred, 0):
        try:
            with socket.socket() as s:
                s.bind(("127.0.0.1", port))
                return s.getsockname()[1]
        except OSError:
            continue
    raise RuntimeError("빈 포트가 없습니다")


class Demo:
    def __init__(self, quiet: bool = False, interactive: bool = False, connectors: list | None = None):
        self.quiet = quiet
        self.interactive = interactive
        self.work = tempfile.mkdtemp(prefix="connector-guard-")
        self.issuer, self.issuer_state = start_issuer(0)
        self.ui_port = free_port(0 if quiet else 8765)
        env = dict(os.environ, PYTHONPATH=ROOT, PYTHONIOENCODING="utf-8", GUARD_WORKDIR=self.work,
                   GUARD_UI_PORT=str(self.ui_port),
                   ISSUER_URL=f"http://127.0.0.1:{self.issuer.server_address[1]}")
        if connectors:
            env["GUARD_CONNECTORS"] = json.dumps(connectors, ensure_ascii=False)
        # 데모 에이전트가 감시 프록시에 MCP로 연결한다(①)
        self.agent = StdioClient([sys.executable, "-m", "guard.proxy"], env=env, cwd=ROOT)
        self.agent.initialize("agent-1")
        self.ev_seen = self.nt_seen = 0
        self.log: list[dict] = []   # 테스트용 전체 기록

    # ---------------- 출력 도우미 ----------------
    def say(self, text: str = "") -> None:
        if not self.quiet:
            print(text)

    def scene(self, no: int, title: str) -> None:
        self.say(f"\n{'=' * 64}\n 장면 {no}. {title}\n{'=' * 64}")

    def flush(self, only: str | None = None) -> dict:
        """새로 생긴 이벤트·알림을 출력하고 기록한다."""
        d = self.agent.request("x-admin/dump", {"since_event": self.ev_seen, "since_notice": self.nt_seen})
        if d["events"]:
            self.ev_seen = d["events"][-1]["id"]
        if d["notifications"]:
            self.nt_seen = d["notifications"][-1]["id"]
        self.log.append(d)
        start = "2026-01-01"
        import datetime as dt
        dn = lambda day: (dt.date.fromisoformat(day) - dt.date.fromisoformat(start)).days  # noqa: E731
        for e in d["events"]:
            if only and e["connector_id"] not in (only, None):
                continue
            detail = e["detail"]
            try:
                detail = json.loads(detail)
            except Exception:  # noqa: BLE001
                pass
            self.say(f"  [Day {dn(e['day']):>3}] {e['connector_id'] or '-':<5} "
                     f"{EVENT_TEXT.get(e['type'], e['type'])} ({e['actor']}) {self._fmt(detail)}")
        for n in d["notifications"]:
            if only and n["connector_id"] != only:
                continue
            self.say(f"  [Day {dn(n['day']):>3}] ✉ {n['channel']}: {n['text']}")
        return d

    @staticmethod
    def _fmt(detail) -> str:
        if not detail:
            return ""
        if isinstance(detail, dict):
            return "· " + ", ".join(f"{k}={v}" for k, v in detail.items())
        return f"· {detail}"

    # ---------------- 동작 도우미 ----------------
    def call(self, tool: str, source: str, args: dict | None = None) -> dict:
        res = self.agent.request("tools/call", {"name": tool, "arguments": args or {},
                                                "_meta": {"callSource": source}})
        mark = "✗" if res.get("isError") else "✓"
        self.say(f"  → 호출 {tool} [{source}] {mark} {res['content'][0]['text'][:90]}")
        return res

    def goto(self, day: int) -> None:
        cur = self.agent.request("x-admin/dump", {"since_event": 10**9, "since_notice": 10**9})["day"]
        if day > cur:
            self.agent.request("x-admin/advance", {"days": day - cur})
        self.say(f"  ⏱ 가상 시계 → Day {day}")

    def state(self, cid: str) -> dict:
        d = self.agent.request("x-admin/dump", {"since_event": 10**9, "since_notice": 10**9})
        return next(c for c in d["connectors"] if c["id"] == cid)

    def ui_get(self) -> str:
        with urllib.request.urlopen(f"http://127.0.0.1:{self.ui_port}/") as r:
            return r.read().decode("utf-8")

    def ui_post(self, nonce: str, decision: str) -> int:
        data = urllib.parse.urlencode({"nonce": nonce, "decision": decision}).encode()
        req = urllib.request.Request(f"http://127.0.0.1:{self.ui_port}/decision", data=data)

        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *a, **k):
                return None
        opener = urllib.request.build_opener(NoRedirect)
        try:
            return opener.open(req).status
        except urllib.error.HTTPError as e:
            return e.code

    # ---------------- 장면 ----------------
    def run(self) -> dict:
        out: dict = {}
        self.say(f"작업 폴더: {self.work}")
        self.say(f"재확인 화면: http://127.0.0.1:{self.ui_port}/")

        self.scene(1, "커넥터 등록 → 위험 등급 산정(판별 불가 시 고위험)")
        self.flush()
        out["risk"] = {c: self.state(c)["risk"] for c in ("notes", "crm")}
        self.call("notes__list_notes", "user")
        self.call("crm__list_contacts", "scheduled")

        self.scene(2, "임계값 30일 전 사전 고지 → 임계값 도달 시 일시정지, 구성 정보 보존")
        self.goto(60)
        self.flush(only="notes")
        self.goto(90)
        self.flush(only="notes")
        st = self.state("notes")
        out["notes_paused_day90"] = st["state"]
        out["notes_config_kept"] = st["config"] is not None
        self.say(f"  · notes 상태={st['state']}, 구성 정보 보존={st['config'] is not None}")

        self.scene(3, "정지된 커넥터 요청 → 재확인 카드 → [계속 사용] → 원래 요청 재실행")
        self.goto(100)
        first = self.call("notes__list_notes", "user")
        out["card_blocked"] = first["isError"]
        self.flush(only="notes")
        if self.interactive:
            self.say(f"\n  🧑 브라우저에서 http://127.0.0.1:{self.ui_port}/ 를 열고 [계속 사용]을 눌러 주세요.")
            self.say("     (누를 때까지 기다립니다)")
            while self.state("notes")["state"] == "paused":
                time.sleep(0.5)
            nonce = None
        else:
            page = self.ui_get()                           # 이용자가 자기 화면을 연다
            nonce = re.search(r'name="nonce" value="([^"]+)"', page).group(1)
            self.say("  🧑 이용자가 재확인 화면에서 [계속 사용]을 누름")
            out["keep_status"] = self.ui_post(nonce, "keep")
        self.flush(only="notes")
        retry = self.call("notes__list_notes", "user")  # 에이전트가 원래 요청을 다시 실행
        out["retry_ok"] = not retry["isError"]
        if nonce:
            out["nonce_reuse_status"] = self.ui_post(nonce, "keep")  # 같은 값 재사용은 거부
            self.say(f"  · 같은 재확인 값 재사용 → HTTP {out['nonce_reuse_status']}")

        self.scene(4, "예약 작업의 호출은 건너뜀 + 알림 1회, 에이전트 자율 호출은 차단만")
        self.say("  crm은 120일 주기 예약 작업으로 계속 쓰이지만, 마지막 확인 후 12개월이 지나면 재확인 대상")
        for day in (120, 180, 240, 300, 360):
            self.goto(day)
            if day % 120 == 0:
                self.call("crm__list_contacts", "scheduled")
            self.call("notes__list_notes", "user")      # 메모는 이용자가 계속 사용 중
            self.flush(only="crm")
        self.goto(365)
        self.flush(only="crm")
        out["crm_pause_reason"] = self.state("crm")["pause_reason"]
        self.goto(370)
        self.call("crm__list_contacts", "scheduled")
        d1 = self.flush(only="crm")
        self.goto(371)
        self.call("crm__list_contacts", "scheduled")
        self.call("crm__list_contacts", "autonomous")
        d2 = self.flush(only="crm")
        kinds = [n["kind"] for d in (d1, d2) for n in d["notifications"] if n["connector_id"] == "crm"]
        out["sched_alerts"] = kinds.count("scheduled_skipped")
        out["other_alerts_after_pause"] = len(kinds) - kinds.count("scheduled_skipped")

        self.scene(5, "에이전트가 재확인을 대신 승인하려는 시도 → 거부 및 이력 기록")
        r1 = self.call("guard__reconfirm", "autonomous", {"connector": "crm"})
        try:
            self.agent.request("x-guard/reconfirm", {"connector": "crm"})
            r2_denied = False
        except Exception as e:  # noqa: BLE001
            r2_denied = True
            self.say(f"  → 프로토콜 메서드 x-guard/reconfirm ✗ {e}")
        forged = self.ui_post("guessed-value", "keep")  # 1회용 값 없이 화면 요청 위조
        self.say(f"  → 1회용 값 없이 재확인 요청 위조 → HTTP {forged}")
        d = self.flush()
        out["agent_denied"] = r1["isError"] and r2_denied and forged == 403
        out["denied_events"] = sum(1 for e in d["events"] if e["type"] == "reconfirm_denied")
        out["crm_still_paused"] = self.state("crm")["state"] == "paused"

        self.scene(6, "무응답 지속 → 최종 고지 → 해제 → 발급기관 폐기 후 토큰 무효 확인")
        self.goto(515)
        self.flush(only="crm")
        self.goto(545)
        d = self.flush(only="crm")
        cascade = next((json.loads(e["detail"]) for e in d["events"]
                        if e["connector_id"] == "crm" and e["type"] == "revoke_cascade"), {})
        st = self.state("crm")
        out.update(crm_state=st["state"], crm_config_deleted=st["config"] is None,
                   revoked=cascade.get("revoked"), introspect_active=cascade.get("introspect_active"),
                   vault_cleared=cascade.get("vault_cleared"))
        self.say(f"  · crm 상태={st['state']}, 구성 정보 삭제={st['config'] is None}, "
                 f"발급기관 조회 결과 active={cascade.get('introspect_active')}")
        after = self.call("crm__list_contacts", "user")
        tools = [t["name"] for t in self.agent.request("tools/list")["tools"]]
        out["released_blocked"] = after["isError"]
        out["crm_tools_hidden"] = not any(t.startswith("crm__") for t in tools)
        self.say(f"  · 에이전트가 보는 도구 목록(해제된 crm 도구는 사라짐): {tools}")
        self.say("\n시연 완료.")
        return out

    def close(self) -> None:
        self.agent.close()
        self.issuer.shutdown()


if __name__ == "__main__":
    demo = Demo(interactive="--interactive" in sys.argv)
    try:
        demo.run()
        if "--keep" in sys.argv or "--interactive" in sys.argv:
            print(f"\n재확인 화면을 띄워 둡니다: http://127.0.0.1:{demo.ui_port}/  (종료: Ctrl+C)")
            while True:
                time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        demo.close()
