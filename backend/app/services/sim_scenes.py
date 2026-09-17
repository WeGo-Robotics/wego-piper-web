"""시뮬 장면 스토어 — 사람이 만든 세계를 파일로 (feature/sim-scene-editor.md §6).

## 정본은 파일 하나

장면 하나가 JSON 파일 하나다. 그래서 "환경 불러오기/내보내기"가 **파일을 주고받는 일**이
되고, 기계 사이 이사·공유·백업이 전부 같은 한 가지 동작이 된다.

## 자리는 데이터 루트, wheel 이 아니다

`settings.sim_scenes_dir`(= `config_dir/sim_scenes`, 컨테이너에선 `/data/config/sim_scenes`).
패키지 안(`piper_sim/assets/`)에 두면 고칠 때마다 릴리스가 필요하고, 릴리스를 건너뛴
호스트는 영영 옛 세계를 본다 — .120 이 v0.5.4 게이트웨이에 0.4.7 wheel 로 옛 장면을
렌더한 사건(2026-09-16)이 그 값이다.

## 데몬에는 **id 가 아니라 명세를 보낸다**

⚠ 컨테이너는 이 디렉토리를 `/data/config/sim_scenes` 로, 호스트(simd)는
`/srv/piper-data/config/sim_scenes` 로 본다 — **같은 id 가 서로 다른 경로**다. 데몬에게
"3번 장면을 올려라"라고 말하면 그 차이가 언젠가 조용히 문다. 게이트웨이가 파일을 읽어
**명세 dict 를 통째로** 넘긴다. 데몬은 파일 시스템을 안 본다.

그 대신 simd 가 재시작하면 기본 장면으로 돌아온다 — 그 불일치는 `ensure_applied()` 가
보고 다시 올린다(카메라 프로파일을 연결 때 다시 적용하는 것과 같은 규율).
"""

import json
import logging
import re
import time
from pathlib import Path

from piper_sim import scene_spec

from app.core.config import settings

logger = logging.getLogger(__name__)

#: 적용 중인 장면 id 를 적어 두는 자리. 게이트웨이가 재시작해도 무엇을 올렸는지 안다.
CURRENT = ".current"
MAX_BYTES = 1 << 20      # 장면 JSON 1MB — 프리미티브 수천 개도 그 안이다


class SceneStoreError(ValueError):
    """사람이 고칠 수 있는 실패 — 라우터가 400 으로 돌려준다."""


def _dir() -> Path:
    d = settings.sim_scenes_dir
    d.mkdir(parents=True, exist_ok=True)
    return d


def slug(name: str) -> str:
    """이름 → 파일명. 한글 이름이 흔하므로 **비면 시간으로 짓는다** (빈 파일명 금지)."""
    s = re.sub(r"[^a-z0-9-]+", "-", str(name).strip().lower()).strip("-")
    return s[:32] or f"scene-{int(time.time())}"


def _path(sid: str) -> Path:
    # ⚠ 경로 조작 방지 — id 는 파일명 한 조각이다. `../` 이 들어오면 데이터 루트 밖을 쓴다.
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", str(sid or "")):
        raise SceneStoreError(f"장면 id 가 이상합니다: {sid!r}")
    return _dir() / f"{sid}.json"


def current_id() -> str | None:
    f = _dir() / CURRENT
    return f.read_text(encoding="utf-8").strip() or None if f.exists() else None


def read(sid: str) -> dict:
    p = _path(sid)
    if not p.exists():
        raise SceneStoreError(f"'{sid}' 장면이 없습니다")
    try:
        return scene_spec.load(p)
    except scene_spec.SceneError as exc:
        raise SceneStoreError(str(exc))


def listing() -> list[dict]:
    """목록 — 깨진 파일도 **숨기지 않고** 왜 못 읽는지 달아 준다.

    ⚠ 조용히 건너뛰면 사람이 만든 장면이 사라진 것처럼 보인다. 목록에서 사라진 파일은
    아무도 못 고친다.
    """
    cur = current_id()
    out = []
    for p in sorted(_dir().glob("*.json")):
        sid = p.stem
        row = {"id": sid, "applied": sid == cur, "updated_at": p.stat().st_mtime}
        try:
            spec = scene_spec.load(p)
            # 장면 파일은 기계 사이를 오가는데 **메시는 따라오지 않는다**(§4). 적용을
            # 눌러 보고서야 아는 대신 목록에서 미리 말한다.
            from app.services import sim_assets
            row |= {"name": spec["name"], "count": len(spec["objects"]), "error": None,
                    "missing_assets": sim_assets.missing(spec)}
        except scene_spec.SceneError as exc:
            row |= {"name": sid, "count": 0, "error": str(exc)}
        out.append(row)
    return out


def save(sid: str, spec: dict) -> dict:
    """검사한 뒤 저장한다 — **깨진 장면은 디스크에 안 남는다.**

    저장은 임시 파일 → `replace` 다. 도중에 죽어도 옛 장면이 반쯤 덮여 남지 않는다.
    """
    try:
        v = scene_spec.validate(spec)
    except scene_spec.SceneError as exc:
        raise SceneStoreError(str(exc))
    v["id"] = sid
    p = _path(sid)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(v, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(p)
    logger.info("장면 저장: %s (%s, 물체 %d)", sid, v["name"], len(v["objects"]))
    return {"id": sid, "name": v["name"], "count": len(v["objects"])}


def delete(sid: str) -> bool:
    p = _path(sid)
    if not p.exists():
        return False
    if current_id() == sid:
        raise SceneStoreError(f"'{sid}' 는 지금 올라간 장면입니다 — 다른 장면을 먼저 적용하세요")
    p.unlink()
    return True


def import_text(text: str, name: str = "") -> dict:
    """**환경 불러오기** — 파일 내용을 받아 검사하고 새 장면으로 저장한다.

    ⚠ 검사를 통과해야 디스크에 닿는다. 사람이 손으로 쓰거나 다른 기계에서 가져온 파일이라
    깨져 있을 수 있고, 그대로 받아 두면 목록에서만 보이고 못 올리는 장면이 쌓인다.
    """
    if len(text.encode("utf-8")) > MAX_BYTES:
        raise SceneStoreError(f"장면 파일이 너무 큽니다 ({MAX_BYTES // 1024}KB 넘음)")
    try:
        raw = json.loads(text)
    except json.JSONDecodeError as exc:
        raise SceneStoreError(f"JSON 을 읽을 수 없습니다 ({exc.lineno}번째 줄): {exc.msg}")
    if not isinstance(raw, dict):
        raise SceneStoreError("장면이 JSON 객체가 아닙니다")
    if name:
        raw = dict(raw, name=name)
    sid = slug(raw.get("id") or raw.get("name") or "")
    base, n = sid, 2
    while _path(sid).exists():          # 이름이 겹치면 덮지 않는다 — 가져오기는 추가다
        sid, n = f"{base}-{n}", n + 1
    return save(sid, raw)


def busy_reason() -> str | None:
    """지금 장면을 갈아끼우면 안 되는 이유 — 있으면 그 말, 없으면 None.

    ⚠ 교체는 **모델 재컴파일 + MjData 신규**다. 에피소드 한가운데 하면 앞뒤가 다른 세계에서
    모인 데이터가 되고, 관측이 바뀐 것을 라벨은 모른다 — 나중에 걸러낼 방법이 없다.

    ⚠ 조종 창(웹 리더)은 `Activity.TELEOP` 에 안 잡힌다. 그 활동의 상태 제공자는 CLI
    텔레옵 세션(`teleop_session`)이고 웹 리더는 자기 서비스라서다. 시뮬을 모는 사람은
    십중팔구 조종 창을 쓰므로 **여기서 따로 본다** — 안 보면 팔을 몰고 있는 사람 밑에서
    세계가 사라진다.
    """
    from app.services import exclusivity as X
    from app.services.web_leader import web_leader

    why = X.blocked_reason(X.Activity.SCENE_SWAP)
    if why:
        return why
    return "조종 창" if web_leader.is_running else None


def apply(sid: str) -> dict:
    """장면을 simd 에 올린다 — 실패하면 **`current` 를 안 바꾼다**.

    순서가 중요하다: 먼저 올리고 나중에 기록한다. 반대로 하면 못 올린 장면이 "적용됨"으로
    남아 `ensure_applied()` 가 매번 같은 실패를 되풀이한다.
    """
    from app.services import sim_robot_client as sim

    if (why := busy_reason()):
        raise SceneStoreError(f"{why} 중에는 장면을 바꿀 수 없습니다 — 먼저 끝내세요")
    spec = read(sid)
    result = sim.call_strict("load_scene", spec, timeout=30)
    (_dir() / CURRENT).write_text(sid, encoding="utf-8")
    logger.info("장면 적용: %s", sid)
    return {"id": sid, "applied": True, **(result or {})}


def ensure_applied() -> dict | None:
    """simd 가 도는 장면이 우리가 적용한 것과 다르면 다시 올린다.

    ⚠ **데몬은 재시작하면 기본 장면으로 돌아온다** — 명세를 메모리에만 들고 있기 때문이다
    (그게 §6 의 경로 문제를 피하는 대가다). 그 사실을 아무도 안 보면 사람은 자기 장면이
    올라가 있다고 믿은 채 엉뚱한 세계에서 수집한다. 게이트웨이 기동과 시뮬 팔 연결에서 부른다.
    """
    from app.services import sim_robot_client as sim

    sid = current_id()
    if not sid or not sim.sim_available():
        return None
    try:
        live = sim.call("scene", default=None)
        want = read(sid)
        if live and (live.get("spec") or {}).get("objects") == want["objects"]:
            return None                          # 이미 그 세계다
        out = apply(sid)
        logger.info("시뮬 장면을 다시 올렸다: %s (데몬이 기본 장면으로 돌아와 있었다)", sid)
        return out
    except Exception as exc:                     # 기동을 막지 않는다 — 말만 한다
        logger.warning("시뮬 장면 재적용 실패 (%s): %s", sid, exc)
        return None


def export_text(sid: str) -> str:
    """**환경 내보내기** — 파일로 내려받을 내용. 들여쓰기를 남겨 사람이 열어 고칠 수 있게."""
    return json.dumps(read(sid), ensure_ascii=False, indent=2)
