"""메시 자산 — 사람이 올린 물건을 **양쪽이 같은 자리에서** 본다
(feature/sim-scene-editor.md §4 · 3단계).

## 왜 한 모듈인가

게이트웨이(컨테이너)가 **쓰고**, simd(호스트)가 **읽는다**. 두 구현으로 나누면 디렉토리
구조·메타 키가 갈리고, 그 어긋남은 "올렸는데 시뮬에 안 보인다"로만 드러난다. 여기 하나만
둔다 — 게이트웨이 이미지에도 `piper_sim` 이 들어 있다(`--no-deps`, mujoco 없이).

## ⚠ 경로는 id 가 아니라 **각자의 루트 + 같은 상대경로**

가상환경 명세는 데몬에 dict 로 통째로 간다(§6). 하지만 메시는 파일이라 그럴 수 없다 — 몇 MB 를
버스로 보낼 수는 없다. 그래서 **자산 id** 만 싣고 양쪽이 **자기 루트**에서 푼다:

| | 루트 | 왜 |
|---|---|---|
| 게이트웨이(컨테이너) | `PIPER_CONFIG_DIR` = `/data/config` | Dockerfile 의 경로 계약 |
| simd(호스트) | `PIPER_CONFIG_DIR` (유닛이 박는다) → `PIPER_DATA_ROOT/config` → `/srv/piper-data/config` → `~/.config/piper-web` | compose 가 그 디렉토리를 `/data` 로 마운트한다 |

둘은 **같은 물리 디렉토리**다. 한쪽 경로 문자열을 다른 쪽에 넘기는 일만 안 하면 된다.

## 단위는 사람에게 묻는다

OBJ·STL 에는 단위가 없다. mm 로 뜬 모델을 그대로 쓰면 1000배로 선다 — 시스템이 알아낼
방법이 **없다**. 올릴 때 잰 크기를 mm 로 보여 주고 확인받아 `unit_scale` 로 적는다.
"""

import hashlib
import json
import logging
import os
import re
import time
from pathlib import Path

from piper_sim import mesh as _mesh

logger = logging.getLogger(__name__)

ID_RE = re.compile(r"^[0-9a-f]{16}$")
#: 메시 파일 상한 — 스캔 원본은 수십 MB 가 예사다. 그대로 두면 컴파일도 렌더도 기어간다.
MAX_BYTES = 32 << 20
MAX_FACES = 200_000      # MuJoCo STL 디코더의 상한과 같다


class AssetError(ValueError):
    """사람이 고칠 수 있는 실패 — 무엇으로 바꿔 오면 되는지까지 적는다."""


#: 게이트웨이가 **자기 설정으로** 박아 주는 루트. 추정과 갈리지 않게 하는 안전장치다.
#: ⚠ 한 기계에 배포본과 개발 체크아웃이 같이 있으면 추정이 갈린다 — `/srv/piper-data` 가
#:   존재하니 데몬 쪽 규칙은 그걸 고르는데, 개발 게이트웨이는 `~/.config/piper-web` 을 쓴다.
#:   그러면 "올렸는데 시뮬에 없다"가 된다. 게이트웨이는 추정하지 말고 말해 준다.
_OVERRIDE: Path | None = None


def use_config_dir(path: Path | str | None) -> None:
    """이 프로세스의 설정 디렉토리를 못 박는다 (게이트웨이가 부른다). `None` 이면 푼다."""
    global _OVERRIDE
    _OVERRIDE = Path(path) if path is not None else None


def config_root() -> Path:
    """게이트웨이와 데몬이 같은 디렉토리를 가리키게 하는 규칙 (위 표)."""
    if _OVERRIDE is not None:
        return _OVERRIDE
    if c := os.getenv("PIPER_CONFIG_DIR"):
        return Path(c)
    if d := os.getenv("PIPER_DATA_ROOT"):
        return Path(d) / "config"
    deployed = Path("/srv/piper-data/config")
    return deployed if deployed.is_dir() else Path.home() / ".config" / "piper-web"


def root() -> Path:
    return config_root() / "sim_assets"


def _dir(asset_id: str) -> Path:
    if not ID_RE.match(str(asset_id or "")):
        raise AssetError(f"자산 id 가 이상합니다: {asset_id!r}")
    return root() / asset_id


def meta(asset_id: str) -> dict:
    p = _dir(asset_id) / "meta.json"
    if not p.exists():
        raise AssetError(f"자산 '{asset_id}' 이 이 기계에 없습니다 — 가상환경과 함께 옮겨 오세요")
    return json.loads(p.read_text(encoding="utf-8"))


def mesh_path(asset_id: str) -> Path:
    m = meta(asset_id)
    p = _dir(asset_id) / m["file"]
    if not p.exists():
        raise AssetError(f"자산 '{asset_id}' 의 메시 파일이 없습니다: {m['file']}")
    return p


def listing() -> list[dict]:
    out = []
    r = root()
    if not r.is_dir():
        return out
    for d in sorted(r.iterdir()):
        if not (d / "meta.json").exists():
            continue
        try:
            out.append(json.loads((d / "meta.json").read_text(encoding="utf-8")))
        except json.JSONDecodeError as exc:       # 숨기지 않는다 — 숨으면 아무도 못 고친다
            out.append({"id": d.name, "name": d.name, "error": f"메타를 못 읽습니다: {exc.msg}"})
    return out


def add(data: bytes, filename: str, name: str = "", unit_scale: float | None = None) -> dict:
    """메시를 받아 재고 저장한다 — **못 쓰는 파일은 디스크에 안 남는다.**

    `unit_scale` 을 안 주면 크기로 **추측**해 적어 둔다(§ 아래). 사람이 확인/수정하는 것이
    정답이지만, 기본값이 그럴듯해야 대부분은 그냥 넘어간다.
    """
    ext = Path(filename).suffix.lower()
    if len(data) > MAX_BYTES:
        raise AssetError(f"파일이 너무 큽니다 ({len(data) >> 20}MB) — 상한은 {MAX_BYTES >> 20}MB 입니다. "
                         "Blender 의 데시메이트로 면을 줄여 오세요")
    asset_id = hashlib.sha256(data).hexdigest()[:16]
    d = _dir(asset_id)
    if (d / "meta.json").exists():
        return meta(asset_id)                     # 같은 파일은 다시 안 만든다

    d.mkdir(parents=True, exist_ok=True)
    f = d / f"mesh{ext}"
    f.write_bytes(data)
    try:
        info = _mesh.inspect(f)                   # 여기서 포맷·깨짐을 잡는다
        if info["faces"] > MAX_FACES:
            raise AssetError(f"면이 {info['faces']:,} 개입니다 — MuJoCo 상한은 {MAX_FACES:,} 개입니다. "
                             "Blender 의 데시메이트로 줄여 오세요")
    except (_mesh.MeshError, AssetError):
        f.unlink(missing_ok=True)
        d.rmdir()
        raise
    scale = float(unit_scale) if unit_scale else guess_unit_scale(info["bbox"])
    rec = {
        "id": asset_id, "name": name or Path(filename).stem, "file": f.name,
        "format": ext.lstrip("."), "raw": info, "unit_scale": scale,
        "bbox_m": [v * scale for v in info["bbox"]],
        "bytes": len(data), "added_at": time.time(), "error": None,
    }
    (d / "meta.json").write_text(json.dumps(rec, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info("자산 추가: %s (%s, 면 %d, %.1f×%.1f×%.1f mm)", asset_id, rec["name"],
                info["faces"], *[v * 1000 for v in rec["bbox_m"]])
    return rec


def guess_unit_scale(bbox: list[float]) -> float:
    """단위 추측 — **틀릴 수 있고, 그래서 화면이 확인을 받는다.**

    테이블 위 물건은 10cm 안팎이다. m·cm·mm 셋 중 **그 크기에 로그 거리로 제일 가까운**
    배율을 고른다. 실측: 20×30×40 → mm(4cm), 400×200×150 → mm(40cm), 0.02×0.03×0.04 → m,
    8.2×8.1×9.5 → cm(9.5cm).
    """
    longest = max(bbox) if bbox else 0.0
    if longest <= 0:
        return 1.0
    # ⚠ "범위에 들어오는 첫 배율"은 틀린다 — 파일이 20×30×40 이면 cm(0.4m)도 범위에 드는데
    #   그건 CAD 의 mm(4cm)일 때가 훨씬 흔하다(실제로 그렇게 골랐다). **가장 그럴듯한 크기**
    #   (테이블 위 물건 ≈ 10cm)에 로그 거리로 제일 가까운 배율을 고른다.
    import math

    return min((1.0, 0.01, 0.001), key=lambda k: abs(math.log(longest * k / 0.1)))


def set_unit_scale(asset_id: str, scale: float) -> dict:
    """사람이 확인한 단위. 크기가 바뀌면 이 자산을 쓰는 가상환경이 다 같이 바뀐다 — 맞다."""
    if not 1e-6 <= float(scale) <= 1000.0:
        raise AssetError(f"배율이 이상합니다: {scale}")
    rec = meta(asset_id)
    rec["unit_scale"] = float(scale)
    rec["bbox_m"] = [v * float(scale) for v in rec["raw"]["bbox"]]
    (_dir(asset_id) / "meta.json").write_text(json.dumps(rec, ensure_ascii=False, indent=2),
                                              encoding="utf-8")
    return rec


def rename(asset_id: str, name: str) -> dict:
    rec = meta(asset_id)
    rec["name"] = str(name).strip() or rec["name"]
    (_dir(asset_id) / "meta.json").write_text(json.dumps(rec, ensure_ascii=False, indent=2),
                                              encoding="utf-8")
    return rec


def delete(asset_id: str) -> bool:
    d = _dir(asset_id)
    if not d.is_dir():
        return False
    for p in d.iterdir():
        p.unlink()
    d.rmdir()
    return True
