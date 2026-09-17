"""메시 자산 — 업로드 창구 (feature/sim-scene-editor.md 3단계).

**일은 `piper_sim.assets` 가 한다.** 게이트웨이가 쓰고 simd 가 읽는 저장소라 구현이 하나여야
하고, 그 하나는 양쪽이 다 가진 `piper_sim` 에 있다(이미지에 `--no-deps` 로 들어 있다).
여기 있는 것은 HTTP 가 필요한 것뿐이다: 우리 설정 디렉토리를 알려 주고, 오류를 400 으로
바꿀 수 있게 갈아 끼운다.

⚠ **루트를 추정하게 두지 않는다.** 한 기계에 배포본과 개발 체크아웃이 같이 있으면
`/srv/piper-data` 가 존재해 데몬 쪽 규칙은 그걸 고르는데 개발 게이트웨이는
`~/.config/piper-web` 을 쓴다 — "올렸는데 시뮬에 없다"가 된다. 매번 못 박는다.
"""

from piper_sim import assets as _assets
from piper_sim.assets import AssetError  # noqa: F401  (라우터가 잡는다)

from app.core.config import settings


def store():
    """설정 디렉토리를 맞춘 자산 저장소. **매번** 맞춘다 — 테스트가 설정을 갈아끼운다."""
    _assets.use_config_dir(settings.config_dir)
    return _assets


def listing() -> list[dict]:
    return store().listing()


def add(data: bytes, filename: str, name: str = "", unit_scale: float | None = None) -> dict:
    return store().add(data, filename, name=name, unit_scale=unit_scale)


def meta(asset_id: str) -> dict:
    return store().meta(asset_id)


def set_unit_scale(asset_id: str, scale: float) -> dict:
    return store().set_unit_scale(asset_id, scale)


def rename(asset_id: str, name: str) -> dict:
    return store().rename(asset_id, name)


def delete(asset_id: str) -> bool:
    return store().delete(asset_id)


def missing(spec: dict) -> list[str]:
    """이 장면이 쓰는 자산 중 **이 기계에 없는 것**.

    장면 파일은 기계 사이를 오가는데 메시는 따라오지 않는다(§4). 그 사실을 목록에서
    미리 말해 주지 않으면, 사람은 적용을 눌러 보고서야 안다.
    """
    st = store()
    out = []
    for o in spec.get("objects") or []:
        if o.get("shape") == "mesh" and o.get("asset"):
            try:
                st.meta(o["asset"])
            except AssetError:
                out.append(o["asset"])
    return sorted(set(out))
