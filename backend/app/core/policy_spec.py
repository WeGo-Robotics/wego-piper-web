"""정책 스펙 로더 — `policies/*.yaml` 을 읽고 검증한다.

## 왜 YAML 인가

정책 하나를 추가할 때 손댈 곳이 **여섯 군데**였고 그 중 둘이 TSX 안에 있었다
(feature/policy-ui-spec.md). 파이썬 dict 로 모으면 프론트가 못 읽어서, 화면 쪽에
`policyType === 'smolvla'` 같은 분기가 다시 자란다. 실제로 그렇게 자랐고,
`pi0_fast`·`tdmpc`·`vqbet` 은 백엔드엔 있는데 화면 스키마가 없어 **골라도 아무 일도
안 일어났다.**

## 이 파일이 하지 않는 것

**렌더링을 기술하지 않는다.** `component`/`layout`/`order` 같은 키는 검증에서
막는다 — YAML 로 화면을 그리기 시작하면 타입 검사도 디버깅도 안 되는 JSX 가 된다.
스펙은 *어떤 필드가 있는가*를 말하고, 배치는 화면이 정한다.

## 깨진 파일 하나가 전체를 죽이지 않는다

파싱·검증에 실패한 정책은 **그 정책만 목록에서 빠지고** 로그가 남는다.
반쯤 그려진 폼을 띄우는 것보다 아예 안 보이는 편이 낫다.
"""

import logging
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.core.config import settings

logger = logging.getLogger(__name__)

# 저장소에 실려 나가는 정본. `backend/app/core/` 에서 세 단계 위가 저장소 루트다.
BUILTIN_DIR = Path(__file__).resolve().parents[3] / "policies"
# 기기별 덮어쓰기·추가 (ROADMAP "기기별 설정 분리")
OVERLAY_DIR = settings.config_dir / "policies"

SPEC_VERSION = 1

# ⚠ **표현 계층 키 금지.** 하나라도 들어오면 스펙이 UI 프레임워크로 자라기 시작한다.
FORBIDDEN_KEYS = {"component", "layout", "col", "col_span", "order", "style", "class"}


class _Strict(BaseModel):
    """모르는 키를 거부한다 — 오타가 조용히 무시되면 "왜 안 먹지"가 된다."""
    model_config = ConfigDict(extra="forbid")


class Capabilities(_Strict):
    train: bool = False
    infer: bool = False
    rtc: bool = False
    encoder_probe: bool = False
    # ⚠ `vlm` 유무로 유추하지 않는다 — 둘은 다른 사실이고, VLM 없이 언어를 받는
    # 정책이 나오는 순간 유추가 거짓말이 된다.
    language: bool = False


class Bases(_Strict):
    policy: str = ""
    vlm: str = ""


class Runtime(_Strict):
    """wrapper 가 import 할 클래스. `[모듈, 클래스]` 두 칸."""
    model: list[str] = Field(default_factory=list, min_length=2, max_length=2)
    config: list[str] = Field(default_factory=list, min_length=2, max_length=2)


class Override(_Strict):
    value: Any
    # ⚠ **이유가 없으면 이탈이 아니라 오타다.** 그래서 필수다.
    reason: str = Field(min_length=1)


class TrainField(_Strict):
    key: str
    kind: Literal["number", "bool"]
    label: str = ""          # 비우면 화면이 `key` 를 그대로 쓴다
    default: Any = None
    min: float | None = None
    max: float | None = None
    step: float | None = None
    # 모델 구조를 정하는 값 — 체크포인트에서 이어 학습할 땐 이미 고정이라 가린다
    arch: bool = False
    # 기본값을 LeRobot config 클래스에서 읽어 채운다 (tools/gen_policy_spec.py)
    from_lerobot: bool = False
    override: Override | None = None

    def resolved_default(self) -> Any:
        return self.override.value if self.override else self.default


class Condition(_Strict):
    field: str
    # 문법을 **일부러 빈약하게** 둔다. 임의 표현식을 허용하면 YAML 안에 프로그램이
    # 생기고, 그건 타입 검사도 테스트도 안 되는 코드다. 표현이 안 되면
    # 그건 진짜 로직이라는 신호다 — 화면에 남기고 이유를 적는다.
    is_: Any = Field(default=None, alias="is")
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class Warning_(_Strict):
    when: Condition
    text: str
    level: Literal["info", "warn", "error"] = "warn"
    and_: Condition | None = Field(default=None, alias="and")
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class TrainSpec(_Strict):
    defaults: dict[str, Any] = Field(default_factory=dict)
    fields: list[TrainField] = Field(default_factory=list)
    warnings: list[Warning_] = Field(default_factory=list)


class Tap(_Strict):
    key: str
    label: str
    default: bool = False


class ProbeSpec(_Strict):
    base_label: str = ""
    taps: list[Tap] = Field(default_factory=list)
    note: str = ""


class PolicySpec(_Strict):
    spec_version: int
    type: str
    label: str
    supported: bool = False
    capabilities: Capabilities = Field(default_factory=Capabilities)
    bases: Bases = Field(default_factory=Bases)
    runtime: Runtime = Field(default_factory=Runtime)
    # 베이스 없이 처음부터 학습할 때의 안내. 정책마다 다른 **사실**이라 스펙에 둔다.
    scratch_note: str = ""
    train: TrainSpec = Field(default_factory=TrainSpec)
    encoder_probe: ProbeSpec = Field(default_factory=ProbeSpec)


def _forbidden(node: Any) -> str | None:
    """표현 계층 키가 섞였는지 재귀로 본다."""
    if isinstance(node, dict):
        for k, v in node.items():
            if k in FORBIDDEN_KEYS:
                return k
            if (found := _forbidden(v)) is not None:
                return found
    elif isinstance(node, list):
        for item in node:
            if (found := _forbidden(item)) is not None:
                return found
    return None


def _merge(base: dict, over: dict) -> dict:
    """기기별 덮어쓰기 — dict 는 깊게, 나머지는 통째로 교체.

    리스트를 병합하지 않는 것은 의도다. 필드 목록을 부분 병합하면 "어느 항목이
    어디서 왔나"를 아무도 못 따라간다. 덮어쓸 거면 목록째 덮어쓴다.
    """
    out = dict(base)
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def _read(path: Path) -> dict | None:
    try:
        data = yaml.safe_load(path.read_text()) or {}
    except Exception as exc:
        logger.error("정책 스펙 파싱 실패, 건너뜀: %s — %s", path.name, exc)
        return None
    if not isinstance(data, dict):
        logger.error("정책 스펙이 매핑이 아닙니다, 건너뜀: %s", path.name)
        return None
    return data


def load_specs() -> dict[str, PolicySpec]:
    """`policies/*.yaml` + 기기별 덮어쓰기. 실패한 파일은 그것만 빠진다."""
    raw: dict[str, dict] = {}

    for d in (BUILTIN_DIR, OVERLAY_DIR):
        if not d.is_dir():
            continue
        for f in sorted(d.glob("*.yaml")):
            if f.name.startswith("_"):
                continue          # `_params.yaml` 같은 공용 파일 자리
            data = _read(f)
            if data is None:
                continue
            name = data.get("type") or f.stem
            raw[name] = _merge(raw[name], data) if name in raw else data

    out: dict[str, PolicySpec] = {}
    for name, data in raw.items():
        if (bad := _forbidden(data)) is not None:
            logger.error("정책 %s: 표현 계층 키 %r 는 스펙에 둘 수 없습니다 — "
                         "무엇을 그릴지만 적고 어떻게 그릴지는 화면이 정한다", name, bad)
            continue
        version = data.get("spec_version")
        if version != SPEC_VERSION:
            # 모르는 스키마를 짐작해서 그리지 않는다
            logger.error("정책 %s: spec_version %r 을 모릅니다 (아는 것: %d)",
                         name, version, SPEC_VERSION)
            continue
        try:
            out[name] = PolicySpec(**data)
        except ValidationError as exc:
            logger.error("정책 %s 검증 실패, 건너뜀:\n%s", name, exc)
    return out
