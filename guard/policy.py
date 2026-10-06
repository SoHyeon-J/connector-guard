"""위험 등급과 기간 기준(제안서 3.2·3.3·3.4).

기간은 제안서의 참조 기준선 값이며, 사업자가 서비스 특성에 맞게 바꿀 수 있다.
"""
from __future__ import annotations

# 권한 범위(scope) → (데이터 민감도, 접근 정도). 발급기관이 공개한 분류를 옮겨 적는다고 가정.
SCOPE_CATALOG = {
    "notes.read": ("general", "read"),
    "notes.write": ("general", "write"),
    "calendar.read": ("general", "read"),
    "mail.read": ("sensitive", "read"),
    "mail.send": ("sensitive", "write"),
    "drive.full": ("sensitive", "write"),
    # GitHub OAuth 권한(선택 기능)
    "read:user": ("general", "read"),
    "public_repo": ("general", "write"),
    "repo": ("sensitive", "write"),
}

# [표 3-2] 위험 등급 분류
MATRIX = {
    ("general", "read"): "low",
    ("general", "write"): "mid",
    ("sensitive", "read"): "mid",
    ("sensitive", "write"): "high",
}
ORDER = {"low": 0, "mid": 1, "high": 2}
LABEL = {"low": "저위험", "mid": "중위험", "high": "고위험"}

# [표 3-3] 기간(일)
INACTIVITY_DAYS = {"high": 90, "mid": 90, "low": 180}
ABSOLUTE_CAP_DAYS = 365          # 절대 상한
NOTICE_BEFORE_DAYS = 30          # 사전 고지
RELEASE_AFTER_PAUSE_DAYS = 180   # 일시정지 후 추가 무응답 기간
FINAL_NOTICE_BEFORE_DAYS = 30    # 해제 전 최종 고지
SCHEDULE_MISSES = 2              # 예약 작업: 등록 주기를 2회 연속 넘기면 정지


def risk_of(scopes: list[str]) -> tuple[str, str]:
    """커넥터 단위 위험 등급. 가장 높은 등급을 적용하고, 판별 불가 시 고위험."""
    level, reason = "low", "권한 범위 분류 적용"
    for s in scopes:
        if s not in SCOPE_CATALOG:
            return "high", f"판별 불가 권한 '{s}' → 고위험(안전한 방향)"
        r = MATRIX[SCOPE_CATALOG[s]]
        if ORDER[r] > ORDER[level]:
            level = r
    return level, reason


def inactivity_threshold(risk: str, schedule_days: int | None) -> int:
    """등록된 예약 작업이 있으면 '주기 × 2', 없으면 위험 등급별 기간."""
    if schedule_days:
        return schedule_days * SCHEDULE_MISSES
    return INACTIVITY_DAYS[risk]
