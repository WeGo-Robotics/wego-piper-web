"""HuggingFace 캐시 레이아웃 해석 — 경로 규칙의 단일 정의.

`model_scanner` 와 `dataset_scanner` 에 같은 헬퍼가 쌍둥이로 있었다
(`_latest_snapshot` 은 주석 빼면 완전히 동일, `_repo_id_from_dirname` 은 접두사 한 글자 차이).
그리고 `info.json` 위치 규칙이 세 곳에 적혀 있었는데 **한 곳만 폴백이 없어서**,
평평한 `info.json` 데이터셋이 목록에는 안 보이는데 상세 조회로는 열렸다
(refactor/11-hf-cache-layout.md).

순수 경로 계산이라 의존성이 없다. 로컬 캐시 레이아웃은 **서버가 아니라 클라이언트가
정하므로**, 사내 자체 Hub 로 옮겨도 이 모듈은 그대로 쓴다 (ROADMAP 참고).
"""

import re
from pathlib import Path
from typing import Literal

RepoKind = Literal["models", "datasets"]


def repo_id_from_dirname(dirname: str, kind: RepoKind) -> str | None:
    """`models--org--name` → `org/name`. 형식이 아니면 None."""
    prefix = f"{kind}--"
    if not dirname.startswith(prefix):
        return None
    parts = dirname.split("--", 2)
    if len(parts) < 3:
        return None
    return f"{parts[1]}/{parts[2]}"


def dirname_from_repo_id(repo_id: str, kind: RepoKind) -> str | None:
    """`org/name` → `models--org--name`. `repo_id_from_dirname` 의 역함수."""
    parts = repo_id.split("/")
    if len(parts) != 2:
        return None
    return f"{kind}--{parts[0]}--{parts[1]}"


def latest_snapshot(repo_dir: Path) -> Path | None:
    """`snapshots/` 아래에서 가장 최근 수정된 스냅샷 디렉토리."""
    snapshots_dir = repo_dir / "snapshots"
    if not snapshots_dir.exists():
        return None
    candidates = [d for d in snapshots_dir.iterdir() if d.is_dir()]
    if not candidates:
        return None
    return max(candidates, key=lambda d: d.stat().st_mtime)


def resolve_info_json(root: Path) -> Path | None:
    """데이터셋 루트에서 `info.json` 을 찾는다. 없으면 None.

    LeRobot v2+ 는 `meta/info.json` 이고, 평평한 `info.json` 은 구버전 잔재다.
    **폴백 규칙이 여기 한 곳에만 있어야 한다** — 목록과 상세가 다른 규칙을 쓰면
    "목록엔 없는데 열리는 데이터셋"이 생긴다.
    """
    for candidate in (root / "meta" / "info.json", root / "info.json"):
        if candidate.exists():
            return candidate
    return None


def repo_root_for_delete(path: Path, kind: RepoKind) -> Path:
    """삭제 대상 루트. Hub 스냅샷이면 `snapshots/hash` 의 상위 `models--org--name`.

    **잘못된 폴더를 지울 수 있는 경로**라 레이아웃 지식이 흩어지면 특히 위험하다.
    """
    prefix = f"{kind}--"
    for parent in path.parents:
        if parent.name.startswith(prefix):
            return parent
    return path


# ── repo_id 형식 ─────────────────────────────────────────────────────────────

# HuggingFace repo id 는 `<네임스페이스>/<이름>` 이다. 문자셋은 영숫자와 `-_.`.
# `\w` 는 유니코드를 먹어 한글이 통과한다 — 문자셋을 ASCII 로 못 박는다.
_REPO_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9][A-Za-z0-9._-]*$")


def repo_id_error(repo_id: str) -> str | None:
    """형식이 틀렸으면 사람이 읽을 사유, 맞으면 None.

    LeRobot 의 `sanity_check_dataset_name` 이 `repo_id.split("/")` 를 2개로 언패킹한다.
    슬래시가 없으면 **녹화 시작 직후 ValueError 로 죽는데**, 그 시점엔 이미 팔·카메라를
    다 잡은 뒤라 스택트레이스만 남고 이유를 알기 어렵다. 시작 전에 여기서 막는다.
    """
    name = (repo_id or "").strip()
    if not name:
        return "데이터셋 이름이 필요합니다."
    if "/" not in name:
        return (
            f"데이터셋 이름은 '네임스페이스/이름' 형식이어야 합니다 (입력: {name!r}). "
            f"예: my-org/{name}"
        )
    if name.count("/") > 1:
        return f"슬래시는 하나만 쓸 수 있습니다 (입력: {name!r})."
    if not _REPO_ID_RE.match(name):
        return (
            f"데이터셋 이름에 쓸 수 없는 문자가 있습니다 (입력: {name!r}). "
            "영문·숫자와 -_. 만 쓸 수 있고 각 부분은 영문·숫자로 시작해야 합니다."
        )
    return None
