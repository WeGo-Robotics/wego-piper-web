"""프리셋 공통 스토어 — 이름 붙인 설정 묶음을 저장·복원한다.

프리셋 성격의 저장소가 이미 여섯 군데에 흩어져 있었다 (feature/parameter-presets.md):
로봇만 서버에 이름 있는 프리셋이었고 추론·학습·녹화 설정은 **브라우저 localStorage 의
이름 없는 프리셋 1개**였다. 브라우저를 바꾸면 사라지고, 로봇마다 인스턴스인데
설정이 브라우저에 있으면 로봇↔설정 대응이 깨진다.

도메인마다 다른 것은 `values` 의 스키마와 검증뿐이므로 나머지는 여기서 공유한다.

## scope — 기기별인가 공용인가

로봇마다 별도 인스턴스로 배포하므로 갈린다 (ROADMAP "기기별 설정 분리"):

- `device`: 추론 속도·필터, 카메라 프로파일 — 팔·조명이 기기마다 다르다
- `shared`: 학습 파라미터 — 기기와 무관하고 오히려 로봇 간 공유돼야 한다

지금은 저장 위치가 같지만(`config_dir/presets/`), 나중에 `shared` 만 동기화할 수 있게
필드로 남겨둔다.
"""

import json
import logging
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path

from app.core.config import settings

logger = logging.getLogger(__name__)

PRESETS_ROOT = settings.config_dir / "presets"

# 파일명으로 쓰므로 경로 조작 문자를 막는다
_SAFE_NAME = re.compile(r"^[\w가-힣 .()\-]{1,64}$")

SCOPES = ("device", "shared")


class PresetError(ValueError):
    """잘못된 이름·도메인 등. 라우터가 400 으로 바꾼다."""


@dataclass
class Preset:
    domain: str
    name: str
    values: dict
    scope: str = "device"
    version: int = 1
    note: str = ""
    # 정책 타입에 따라 유효한 값이 달라지는 도메인(학습·추론)에서 쓴다.
    # 다른 정책에 로드하면 공통 항목만 적용하고 나머지는 무시했다고 알린다.
    policy_type: str = ""
    updated_at: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class ApplyReport:
    """프리셋 적용 결과. **조용히 버리면 "저장한 값이 왜 다르지"가 된다.**"""

    values: dict = field(default_factory=dict)
    missing: list[str] = field(default_factory=list)   # 프리셋에 없어 기본값으로 채움
    unknown: list[str] = field(default_factory=list)   # 지금은 없는 파라미터 → 무시
    clamped: list[dict] = field(default_factory=list)  # 범위 밖이라 조정됨
    policy_mismatch: dict | None = None

    def to_dict(self) -> dict:
        return asdict(self)


def _check_name(name: str) -> None:
    if not _SAFE_NAME.match(name or ""):
        raise PresetError(f"프리셋 이름이 올바르지 않습니다: {name!r}")


def _domain_dir(domain: str) -> Path:
    if not domain.isidentifier():
        raise PresetError(f"알 수 없는 도메인: {domain!r}")
    return PRESETS_ROOT / domain


def _path(domain: str, name: str) -> Path:
    _check_name(name)
    return _domain_dir(domain) / f"{name}.json"


def list_presets(domain: str) -> list[dict]:
    """목록 — 값 전체가 아니라 메타만 돌려준다."""
    d = _domain_dir(domain)
    if not d.exists():
        return []
    out = []
    for f in sorted(d.glob("*.json")):
        try:
            data = json.loads(f.read_text())
        except Exception:
            logger.warning("프리셋 파싱 실패, 건너뜀: %s", f)
            continue
        out.append({
            "name": data.get("name", f.stem),
            "scope": data.get("scope", "device"),
            "policy_type": data.get("policy_type", ""),
            "note": data.get("note", ""),
            "updated_at": data.get("updated_at", ""),
        })
    return out


def get(domain: str, name: str) -> Preset | None:
    f = _path(domain, name)
    if not f.exists():
        return None
    data = json.loads(f.read_text())
    data.pop("domain", None)
    return Preset(domain=domain, **data)


def save(
    domain: str,
    name: str,
    values: dict,
    *,
    scope: str = "device",
    note: str = "",
    policy_type: str = "",
) -> Preset:
    _check_name(name)
    if scope not in SCOPES:
        raise PresetError(f"scope 는 {SCOPES} 중 하나여야 합니다: {scope!r}")
    preset = Preset(
        domain=domain, name=name, values=values, scope=scope, note=note,
        policy_type=policy_type,
        updated_at=datetime.now().astimezone().isoformat(timespec="seconds"),
    )
    d = _domain_dir(domain)
    d.mkdir(parents=True, exist_ok=True)
    data = preset.to_dict()
    data.pop("domain")  # 경로가 도메인을 말하므로 파일에는 넣지 않는다
    (d / f"{name}.json").write_text(json.dumps(data, indent=2, ensure_ascii=False))
    logger.info("프리셋 저장: %s/%s", domain, name)
    return preset


def delete(domain: str, name: str) -> bool:
    f = _path(domain, name)
    if not f.exists():
        return False
    f.unlink()
    logger.info("프리셋 삭제: %s/%s", domain, name)
    return True


def apply(
    preset: Preset,
    known_keys: set[str],
    defaults: dict | None = None,
    current_policy_type: str = "",
    bounds: dict[str, dict[str, float]] | None = None,
) -> ApplyReport:
    """프리셋을 현재 스키마에 맞춰 정리한다.

    - 지금 없는 파라미터 → `unknown` 에 담고 버린다
    - 프리셋에 없는 파라미터 → `defaults` 로 채우고 `missing` 에 담는다
    - 범위 밖 값 → 클램프하고 `clamped` 에 담는다 (범위가 바뀌었을 수 있다)
    - 정책 타입이 다르면 `policy_mismatch` 로 알린다 (**조용히 넘기지 않는다**)
    """
    report = ApplyReport()
    bounds = bounds or {}
    for key, value in preset.values.items():
        if key not in known_keys:
            report.unknown.append(key)
            continue
        b = bounds.get(key)
        if b is not None and isinstance(value, (int, float)) and not isinstance(value, bool):
            fixed = max(b["min"], min(b["max"], value))
            if fixed != value:
                report.clamped.append({"key": key, "saved": value, "applied": fixed})
                value = fixed
        report.values[key] = value

    for key, value in (defaults or {}).items():
        if key not in report.values:
            report.values[key] = value
            report.missing.append(key)

    if preset.policy_type and current_policy_type and preset.policy_type != current_policy_type:
        report.policy_mismatch = {
            "saved": preset.policy_type,
            "current": current_policy_type,
        }
    report.unknown.sort()
    report.missing.sort()
    return report
