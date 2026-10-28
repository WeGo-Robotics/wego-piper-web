"""`plugin.yaml` 의 모양 (feature/plugins.md §4). 게이트웨이와 `piper-plugin check` 가 **같은 모델**을 쓴다.

권한 범위(`permissions`)의 이름이 실재하는지는 게이트웨이만 안다(`core/plugin_scopes.py`) —
여기서는 구조만 본다. `check` 는 게이트웨이가 닿으면 범위도 물어본다.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator

MANIFEST = "plugin.yaml"
# 이름이 유닛 이름·URL·버스 키·디렉토리 이름이 된다 — 스트림 이름과 같은 규칙
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,39}$")


class ConfigField(BaseModel):
    key: str
    kind: Literal["number", "bool", "text", "select", "model", "dataset", "camera"] = "text"
    default: float | int | bool | str | None = None
    min: float | None = None
    max: float | None = None
    step: float | None = None
    label: str = ""
    options: list[str] = []
    multiline: bool = False


class CameraSlot(BaseModel):
    key: str
    label: str = ""
    required: bool = True


class ArmSlot(BaseModel):
    """상태를 **읽을** 팔 하나 (feature/plugins.md §7-5). 값은 CAN 인터페이스(`can0`)다.

    읽기 전용이다 — 팔을 움직이는 길은 여전히 추론(REST)뿐이다.
    """
    key: str
    label: str = ""
    required: bool = True


class Resources(BaseModel):
    #: GPU 를 **쓸 수 있는** 플러그인인가. 실제로 쓰는지는 시작 시점의 설정이 정한다 — `gpu_field` 로 지정한
    #: 설정 값(기본 `device`)이 `cpu` 로 시작하면 그 실행은 GPU 플러그인이 아니다(학습과 배타가 아니다)
    gpu: bool = False
    gpu_field: str = "device"
    heartbeat: bool = False
    memory: str | None = None   # systemd MemoryMax (6단계)
    cpu: str | None = None      # systemd CPUQuota (6단계)


class InputSlot(BaseModel):
    """다른 플러그인(또는 검출기)의 출력을 받는 자리 (feature/plugins.md §7-3).

    `kind: detections` 는 버스 검출(`piper:vision:<source>`), `kind: topic` 은 플러그인 토픽
    (`piper:plugin:topic:<source>`). `source` 는 기본값 — 시작할 때 화면이 살아 있는 것들 중에서 고른다.
    """
    key: str
    kind: Literal["detections", "topic"] = "detections"
    source: str = ""
    label: str = ""
    required: bool = True


class Outputs(BaseModel):
    previews: list[str] = []
    detections: list[str] = []
    topics: list[str] = []
    state: list[dict] = []


class Manifest(BaseModel):
    name: str
    version: str = "0.0.0"
    title: str = ""
    description: str = ""
    entry: str = "main.py"
    python: Literal["system", "venv", "venv-isolated"] = "system"
    cameras: list[CameraSlot] = []
    arms: list[ArmSlot] = []
    inputs: list[InputSlot] = []
    config: list[ConfigField] = []
    permissions: list[str] = []
    resources: Resources = Field(default_factory=Resources)
    ui: bool | str = False
    outputs: Outputs = Field(default_factory=Outputs)
    commands: list[str] = []
    mission: bool = False
    #: 같은 폴더를 여러 인스턴스(`<name>:<label>`)로 띄울 수 있다 — 검출기·카메라별 감시 (feature/plugins.md §7-4)
    multi_instance: bool = False

    @field_validator("name")
    @classmethod
    def _name(cls, v: str) -> str:
        if not NAME_RE.match(v):
            raise ValueError("이름은 영문자·숫자·_·- 만, 첫 글자는 영숫자, 1~40자")
        return v

    @model_validator(mode="after")
    def _unique_keys(self):
        for what, items in (("config", self.config), ("cameras", self.cameras), ("arms", self.arms),
                            ("inputs", self.inputs)):
            keys = [i.key for i in items]
            dup = {k for k in keys if keys.count(k) > 1}
            if dup:
                raise ValueError(f"{what} 의 key 가 겹친다: {sorted(dup)}")
        if not self.title:
            self.title = self.name
        return self

    def defaults(self) -> dict:
        return {f.key: f.default for f in self.config if f.default is not None}


class ManifestError(ValueError):
    pass


def load_manifest(folder: Path) -> Manifest:
    """폴더 → Manifest. 깨졌으면 `ManifestError` 에 **줄 번호**까지 담아 던진다."""
    path = folder / MANIFEST
    if not path.is_file():
        raise ManifestError(f"{MANIFEST} 이 없다")
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as e:
        mark = getattr(e, "problem_mark", None)
        where = f" ({mark.line + 1}번째 줄)" if mark is not None else ""
        raise ManifestError(f"{MANIFEST} 문법 오류{where}: {getattr(e, 'problem', e)}") from e
    if not isinstance(raw, dict):
        raise ManifestError(f"{MANIFEST} 은 맵이어야 한다")
    try:
        m = Manifest(**raw)
    except Exception as e:
        raise ManifestError(f"{MANIFEST}: {e}") from e
    if m.name != folder.name:
        raise ManifestError(f"폴더 이름({folder.name})과 매니페스트 name({m.name})이 다르다")
    if not (folder / m.entry).is_file():
        raise ManifestError(f"entry 가 없다: {m.entry}")
    return m
