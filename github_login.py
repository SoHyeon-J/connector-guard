"""GitHub 기기 로그인으로 토큰을 받아 .github_token.json에 저장한다(선택 기능).

사전 준비: .env 파일에 GITHUB_CLIENT_ID, GITHUB_CLIENT_SECRET (README 참고)
    python github_login.py            # 기본 권한: read:user (최소 권한)
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from guard import github as gh  # noqa: E402

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:  # noqa: BLE001
    pass

if __name__ == "__main__":
    gh.load_env()
    scope = sys.argv[1] if len(sys.argv) > 1 else "read:user"
    token = gh.device_login(scope)
    with open(gh.TOKEN_FILE, "w", encoding="utf-8") as f:
        json.dump({"access_token": token, "scope": scope}, f)
    code, me = gh.get(token, "/user")
    print(f"로그인 완료: {me.get('login') if code == 200 else code}. 토큰을 .github_token.json 에 저장했습니다.")
    print("이제 python demo_github.py 를 실행하세요.")
