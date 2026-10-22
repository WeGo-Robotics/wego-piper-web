"""플러그인 실행 토큰의 권한 범위 → 허용되는 (메서드, 경로) (feature/plugins.md §5).

플러그인은 사용자 코드다. 세션 쿠키를 주면 `/api/*` 전체(비밀번호 변경·유닛 종료
포함)가 열리므로, 매니페스트가 **선언한 범위**만 통과시킨다. 표는 여기 한 곳이고
`test_plugin_scopes.py` 가 라우터 목록과 대조한다 — 경로가 바뀌면 여기가 깨진다.

경로 규칙: `/` 로 끝나면 **접두사**(그 아래 전부), 아니면 **정확히 그 경로**.
`"*"` 는 전부 — 단 `/api/auth/*` 는 예외다. 비밀번호를 바꾸거나 로그인을 끄는 것은
사람의 행위다.
"""

from __future__ import annotations

from typing import Final

Rule = tuple[str, str]   # (METHOD, path 또는 path-prefix/)

SCOPES: Final[dict[str, tuple[Rule, ...]]] = {
    # 추론
    "inference.start": (("POST", "/api/models/inference/start"),
                        ("POST", "/api/models/inference/start-custom"),
                        ("POST", "/api/models/inference/preview")),
    "inference.stop": (("POST", "/api/models/inference/stop"),),
    "inference.read": (("GET", "/api/inference/"),
                       ("GET", "/api/models/inference/devices"),
                       ("GET", "/api/models/inference/policy")),
    "models.read": (("GET", "/api/models"), ("GET", "/api/models/")),
    # 실시간 파라미터 — `task` 변경이 여기다 (언어 정책에 판단을 전달하는 길)
    "params.write": (("POST", "/api/params"), ("POST", "/api/params/")),
    "params.read": (("GET", "/api/params/"),),
    # 녹화
    "recording.start": (("POST", "/api/recording/start"),),
    "recording.stop": (("POST", "/api/recording/stop"),),
    "recording.control": (("POST", "/api/recording/task"), ("POST", "/api/recording/skip"),
                          ("POST", "/api/recording/rerecord")),
    "recording.read": (("GET", "/api/recording/"),),
    # 검출·판단
    "vision.read": (("GET", "/api/vision/"),),
    "vision.judge": (("POST", "/api/vision/judge"),),
    "vision.control": (("POST", "/api/vision/start"), ("POST", "/api/vision/stop")),
    # 평가 기록
    "eval.write": (("POST", "/api/eval/log"),),
    "eval.read": (("GET", "/api/eval/stats"),),
    # 상태 조회
    "activity.read": (("GET", "/api/activity"),),
    "plugins.read": (("GET", "/api/plugins"), ("GET", "/api/plugins/")),
}

ALL: Final = "*"
#: `*` 로도 못 가는 곳
NEVER_PREFIXES: Final = ("/api/auth/",)


def known(scope: str) -> bool:
    return scope == ALL or scope in SCOPES


def _rule_matches(rule: Rule, method: str, path: str) -> bool:
    m, p = rule
    if m != method:
        return False
    if p.endswith("/"):
        return path == p[:-1] or path.startswith(p)
    return path == p


def allows(scopes: set[str] | frozenset[str], method: str, path: str) -> bool:
    """이 범위 묶음이 이 요청을 통과시키는가."""
    method = method.upper()
    if any(path.startswith(p) for p in NEVER_PREFIXES):
        return False
    if ALL in scopes:
        return True
    for s in scopes:
        for rule in SCOPES.get(s, ()):
            if _rule_matches(rule, method, path):
                return True
    return False


def describe(scopes: list[str]) -> list[dict]:
    """화면의 권한 배지 재료 — 범위마다 어떤 경로가 열리는지."""
    out = []
    for s in scopes:
        if s == ALL:
            out.append({"scope": s, "rules": [["*", "/api/* (auth 제외)"]]})
        else:
            out.append({"scope": s, "rules": [list(r) for r in SCOPES.get(s, ())]})
    return out
