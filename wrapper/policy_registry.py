"""`policies/*.yaml` 을 wrapper 쪽에서 읽는다 — 클래스 경로와 프로브 tap.

## 왜 백엔드 로더를 안 쓰나

wrapper 는 **백엔드와 다른 파이썬**에서 돌고(`settings.local_python`), 컨테이너로도
나간다. `app.core.policy_spec` 을 import 하면 게이트웨이 패키지를 통째로 끌고 와야
하는데, 그건 이 저장소가 지키는 경계를 깨는 일이다
(`lerobot_bootstrap.py` 머리말과 같은 이유).

그래서 **읽기만** 하는 얇은 판을 따로 둔다. 검증·병합·기본값 채우기는 백엔드가 하고
여기는 파일에 적힌 두 가지만 꺼낸다. 정본이 같은 파일이라 갈라질 여지가 없다 —
예전에 `POLICY_IMPORTS`(wrapper) 와 `POLICIES`(백엔드) 가 각자 목록을 들고 있다가
`sac` 를 학습에서 고를 수 있는데 추론에서 죽는 사고가 났다.
"""

import os
from pathlib import Path

# 컨테이너에서도 같다 — `/app/wrapper/` 옆에 `/app/policies/`.
POLICY_DIR = Path(os.environ.get("PIPER_POLICIES_DIR")
                  or Path(__file__).resolve().parents[1] / "policies")


def _load() -> dict[str, dict]:
    try:
        import yaml
    except ImportError:                       # pragma: no cover - 환경 문제
        return {}
    out: dict[str, dict] = {}
    if not POLICY_DIR.is_dir():
        return out
    for f in sorted(POLICY_DIR.glob("*.yaml")):
        if f.name.startswith("_"):
            continue
        try:
            data = yaml.safe_load(f.read_text()) or {}
        except Exception:
            # 깨진 파일 하나가 wrapper 를 못 뜨게 하지 않는다 — 그 정책만 빠진다
            continue
        if isinstance(data, dict) and data.get("type"):
            out[data["type"]] = data
    return out


SPECS: dict[str, dict] = _load()


def policy_imports() -> dict[str, tuple[str, str, str, str]]:
    """`{정책: (모델모듈, 모델클래스, config모듈, config클래스)}`.

    옛 `POLICY_IMPORTS` 와 **같은 모양**이라 부르는 쪽이 안 바뀐다.
    """
    out = {}
    for name, data in SPECS.items():
        rt = data.get("runtime") or {}
        model, config = rt.get("model") or [], rt.get("config") or []
        if len(model) == 2 and len(config) == 2:
            out[name] = (model[0], model[1], config[0], config[1])
    return out


def config_imports() -> dict[str, str]:
    """`{config클래스이름: 모듈}` — `lerobot_bootstrap` 이 더미 패키지에 등록할 것."""
    return {cfg_cls: cfg_mod
            for _model_mod, _model_cls, cfg_mod, cfg_cls in policy_imports().values()}


def probe_taps(policy_type: str) -> list[dict]:
    return list(((SPECS.get(policy_type) or {}).get("encoder_probe") or {}).get("taps") or [])


def default_tap(policy_type: str) -> str:
    """`default: true` 인 tap. 없으면 첫 번째, 그것도 없으면 빈 문자열.

    예전에는 `args.tap if policy_type == "smolvla" else "backbone"` 이라고
    정책 이름이 박혀 있었다 — 정책이 늘 때마다 여기도 늘어난다.
    """
    taps = probe_taps(policy_type)
    if not taps:
        return ""
    return next((t["key"] for t in taps if t.get("default")), taps[0]["key"])


def tap_keys() -> list[str]:
    """전 정책의 tap 이름 — argparse `choices` 용. 정책별 유효성은 호출부가 본다."""
    return sorted({t["key"] for name in SPECS for t in probe_taps(name)})
