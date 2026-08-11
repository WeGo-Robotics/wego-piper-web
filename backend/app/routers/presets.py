"""프리셋 API — 도메인별 이름 붙인 설정 묶음.

로봇 프리셋([robots.py](robots.py))이 하던 일을 파라미터 도메인으로 일반화했다.
`domain` 은 `robot` / `training` / (나중에) `inference` / `camera`.

> 추론 프리셋은 `PARAM_SPEC`(refactor/01-inference-params.md 단계 2) 이 있어야
> 검증·마이그레이션이 가능하다. 없이 만들면 파라미터 목록이 또 하나 생긴다.
"""

import logging

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.services import presets
from app.services.presets import PresetError

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/presets", tags=["presets"])

# 값 스키마를 아는 도메인 — `apply` 검증에 쓴다.
# 모르는 도메인은 저장/조회만 되고 검증은 통과시킨다.
_KNOWN_KEYS: dict[str, set[str]] = {}


def register_domain(domain: str, known_keys: set[str]) -> None:
    """도메인이 자기 값 스키마를 등록한다."""
    _KNOWN_KEYS[domain] = known_keys


class SaveRequest(BaseModel):
    name: str
    values: dict
    scope: str = "device"
    note: str = ""
    policy_type: str = ""


@router.get("/{domain}")
async def list_domain(domain: str):
    try:
        return presets.list_presets(domain)
    except PresetError as e:
        raise HTTPException(400, str(e))


@router.get("/{domain}/{name}")
async def get_preset(domain: str, name: str):
    try:
        preset = presets.get(domain, name)
    except PresetError as e:
        raise HTTPException(400, str(e))
    if preset is None:
        raise HTTPException(404, "프리셋을 찾을 수 없습니다")
    return preset.to_dict()


@router.post("/{domain}")
async def save_preset(domain: str, body: SaveRequest):
    try:
        preset = presets.save(
            domain, body.name, body.values,
            scope=body.scope, note=body.note, policy_type=body.policy_type,
        )
    except PresetError as e:
        raise HTTPException(400, str(e))
    return preset.to_dict()


@router.delete("/{domain}/{name}")
async def delete_preset(domain: str, name: str):
    try:
        ok = presets.delete(domain, name)
    except PresetError as e:
        raise HTTPException(400, str(e))
    if not ok:
        raise HTTPException(404, "프리셋을 찾을 수 없습니다")
    return {"status": "deleted"}


class ApplyRequest(BaseModel):
    policy_type: str = ""
    defaults: dict = {}


@router.post("/{domain}/{name}/apply")
async def apply_preset(domain: str, name: str, body: ApplyRequest):
    """검증 후 적용 결과를 돌려준다.

    값만 주지 않고 **무엇이 무시됐고 무엇이 기본값으로 채워졌는지**를 함께 준다 —
    조용히 버리면 "저장한 값이 왜 다르지"가 된다.
    """
    try:
        preset = presets.get(domain, name)
    except PresetError as e:
        raise HTTPException(400, str(e))
    if preset is None:
        raise HTTPException(404, "프리셋을 찾을 수 없습니다")

    known = _KNOWN_KEYS.get(domain)
    if known is None:
        # 스키마를 모르는 도메인(로봇 등)은 그대로 돌려준다
        return {"values": preset.values, "missing": [], "unknown": [], "policy_mismatch": None}

    report = presets.apply(preset, known, body.defaults, body.policy_type)
    return report.to_dict()
