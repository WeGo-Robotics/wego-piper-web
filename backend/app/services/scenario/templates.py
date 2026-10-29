"""시나리오 템플릿 로더 — 템플릿 하나 = YAML 하나 + 생성기 모듈 하나 (§4-2).

`policies/*.yaml` 과 같은 관례다: 선언은 YAML, 생성은 Python. YAML 은 **값의 모양**(필드·기본값·
선택지·범위·변형)만 말하고 문구는 화면이 갖는다 — 이유는 `templates/sort.yaml` 머리말.

깨진 파일 하나가 전체를 죽이지 않는다 — 그 템플릿만 목록에서 빠지고 로그가 남는다.
"""

from __future__ import annotations

import importlib
import logging
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.services.scenario import ScenarioError

logger = logging.getLogger(__name__)

TEMPLATE_DIR = Path(__file__).resolve().parent / "templates"
SPEC_VERSION = 1


class _Strict(BaseModel):
    """모르는 키를 거부한다 — 오타가 조용히 무시되면 "왜 안 먹지"가 된다."""
    model_config = ConfigDict(extra="forbid")


class FieldSpec(_Strict):
    # data — 구조가 있는 값(분류 규칙 표 등). 검증은 생성기가 한다.
    kind: Literal["number", "bool", "text", "select", "data"]
    default: Any = None
    options: list[str] = Field(default_factory=list)
    min: float | None = None
    max: float | None = None


class VariantSpec(_Strict):
    default: float = Field(0.0, ge=0.0, le=0.5)
    # episode — 시도마다, round — 라운드(배치)마다
    per: Literal["episode", "round"] = "episode"
    # 값 조건 — 하나라도 안 맞으면 그 변형은 이 설정에서 꺼진다
    requires: dict[str, str] = Field(default_factory=dict)


class TemplateSpec(_Strict):
    spec_version: int
    type: str
    generator: str
    episode_units: list[str] = Field(min_length=1)
    fields: dict[str, FieldSpec] = Field(default_factory=dict)
    variants: dict[str, VariantSpec] = Field(default_factory=dict)

    def to_dict(self) -> dict:
        return self.model_dump()


def load_templates() -> dict[str, TemplateSpec]:
    out: dict[str, TemplateSpec] = {}
    for f in sorted(TEMPLATE_DIR.glob("*.yaml")):
        try:
            data = yaml.safe_load(f.read_text()) or {}
            if data.get("spec_version") != SPEC_VERSION:
                logger.error("시나리오 템플릿 %s: spec_version %r 을 모릅니다", f.name,
                             data.get("spec_version"))
                continue
            spec = TemplateSpec(**data)
            importlib.import_module(f"app.services.scenario.{spec.generator}")
        except (yaml.YAMLError, ValidationError, ImportError) as exc:
            logger.error("시나리오 템플릿 %s 를 건너뜀: %s", f.name, exc)
            continue
        out[spec.type] = spec
    return out


def get_template(name: str) -> TemplateSpec:
    spec = load_templates().get(name)
    if spec is None:
        raise ScenarioError(f"알 수 없는 템플릿입니다: {name}")
    return spec


def generator_of(spec: TemplateSpec):
    return importlib.import_module(f"app.services.scenario.{spec.generator}")


def resolve_fields(spec: TemplateSpec, given: dict) -> dict:
    """템플릿 필드 값 — 빈 것은 기본값, 선택지·범위를 검사한다."""
    out: dict[str, Any] = {}
    for key, f in spec.fields.items():
        v = given.get(key, f.default)
        if f.kind == "number":
            try:
                v = float(v)
            except (TypeError, ValueError):
                raise ScenarioError(f"{key} 는 숫자여야 합니다")
            lo = f.min if f.min is not None else float("-inf")
            hi = f.max if f.max is not None else float("inf")
            if not lo <= v <= hi:
                raise ScenarioError(f"{key} 는 {lo:g}~{hi:g} 사이여야 합니다")
            if v.is_integer():
                v = int(v)
        elif f.kind == "bool":
            v = bool(v)
        elif f.kind == "text":
            v = str(v if v is not None else "")
        elif f.kind == "select":
            if v not in f.options:
                raise ScenarioError(f"{key} 는 {', '.join(f.options)} 중 하나여야 합니다")
        out[key] = v
    return out
