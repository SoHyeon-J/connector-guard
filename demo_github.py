"""실제 GitHub와 연동한 시연(선택 기능).

사전 준비(README의 'GitHub 연동' 참고)
    1) GitHub OAuth App 만들기(Device Flow 사용 설정) → .env에 client id/secret 입력
    2) python github_login.py      # 브라우저에서 코드 입력·승인 → 토큰 저장
실행
    python demo_github.py              # 자동 시연
    python demo_github.py --interactive  # 재확인 카드를 브라우저에서 직접 누름

마지막 장면에서 토큰이 GitHub에서 실제로 폐기되므로, 다시 실행하려면 github_login.py부터 다시 한다.
"""
from __future__ import annotations

import json
import os
import re
import sys
import time

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

from demo import Demo  # noqa: E402
from guard import github as gh  # noqa: E402

CONNECTOR = [{"id": "github", "name": "GitHub", "provider": "github", "resource": "github",
              "scopes": ["read:user"], "schedule_days": None}]


def token_ready() -> bool:
    try:
        with open(os.environ.get("GITHUB_TOKEN_FILE", gh.TOKEN_FILE), encoding="utf-8") as f:
            return bool(json.load(f).get("access_token"))
    except (FileNotFoundError, ValueError):
        return False


def run(demo: Demo) -> dict:
    out: dict = {}
    demo.say(f"재확인 화면: http://127.0.0.1:{demo.ui_port}/")

    demo.scene(1, "GitHub 커넥터 등록 → 위험 등급 산정 → 실제 GitHub 호출")
    demo.flush()
    out["risk"] = demo.state("github")["risk"]
    out["first_call_ok"] = not demo.call("github__get_profile", "user")["isError"]
    demo.call("github__list_repos", "user")

    demo.scene(2, "사전 고지 → 일시정지(구성 정보 보존)")
    demo.goto(150)
    demo.flush()
    demo.goto(180)
    demo.flush()
    out["paused"] = demo.state("github")["state"]

    demo.scene(3, "정지된 커넥터 요청 → 재확인 카드 → [계속 사용] → 실제 GitHub 재호출")
    demo.goto(190)
    demo.call("github__get_profile", "user")
    demo.flush()
    if demo.interactive:
        demo.say(f"\n  🧑 브라우저에서 http://127.0.0.1:{demo.ui_port}/ 를 열고 [계속 사용]을 눌러 주세요.")
        while demo.state("github")["state"] == "paused":
            time.sleep(0.5)
    else:
        nonce = re.search(r'name="nonce" value="([^"]+)"', demo.ui_get()).group(1)
        demo.say("  🧑 이용자가 재확인 화면에서 [계속 사용]을 누름")
        demo.ui_post(nonce, "keep")
    demo.flush()
    out["retry_ok"] = not demo.call("github__get_profile", "user")["isError"]

    demo.scene(4, "에이전트가 재확인을 대신하려는 시도 → 거부")
    demo.call("guard__reconfirm", "autonomous", {"connector": "github"})
    demo.flush()

    demo.scene(5, "무응답 지속 → 최종 고지 → 해제 → GitHub 토큰 실제 폐기 → 무효 확인")
    demo.goto(520)
    demo.flush()
    demo.goto(550)
    d = demo.flush()
    cascade = next((json.loads(e["detail"]) for e in d["events"] if e["type"] == "revoke_cascade"), {})
    out.update(state=demo.state("github")["state"], revoked=cascade.get("revoked"),
               github_status=cascade.get("github_status"), active_after=cascade.get("introspect_active"))
    demo.say(f"  · GitHub 폐기 응답 {cascade.get('github_status')} (204=성공), "
             f"GitHub 토큰 확인 결과 active={cascade.get('introspect_active')}")
    out["after_blocked"] = demo.call("github__get_profile", "user")["isError"]
    demo.say("\n시연 완료. 토큰이 폐기되었으므로 다시 시연하려면 python github_login.py 부터 실행하세요.")
    return out


if __name__ == "__main__":
    gh.load_env()
    if not token_ready():
        print("GitHub 토큰이 없습니다. 먼저 python github_login.py 를 실행하세요.")
        sys.exit(1)
    demo = Demo(interactive="--interactive" in sys.argv, connectors=CONNECTOR)
    try:
        run(demo)
    finally:
        demo.close()
