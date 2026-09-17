"""장면 명세 — 테이블 위 사물을 **JSON 으로 정의하고** MJCF 로 굽는다
(feature/sim-scene-editor.md §4·§5).

## 왜 JSON 이 정본인가

사람이 MuJoCo XML 을 쓰게 하지 않는다. 올린 XML 을 그대로 먹이면 그 문자열이
`<option>`·`<contact>`·액추에이터까지 건드려 **팔의 물리를 바꾼다** — 장면 파일 하나가
로봇 모델을 조용히 망가뜨릴 수 있다는 뜻이다. JSON 은 스키마로 막히고, 폼으로 그릴 수
있고, id 가 안정적이라 데이터셋·평가가 "그 장면의 그 물체"를 가리킬 수 있다.

## 층이 둘이다 — 바탕은 릴리스가, 물체는 사람이

`piper_scene.xml`(wheel 에 실려 오는 **바탕**: 팔·테이블·카메라·조명)을 `MjSpec` 으로
열고 그 위에 JSON 의 물체를 얹어 `compile()` 한다.

⚠ **사람이 올린 사물은 wheel 에 안 들어간다.** .120 은 게이트웨이가 v0.5.4 인데 데몬
wheel 이 0.4.7 이라 옛 장면을 렌더했다(2026-09-16). 장면이 패키지 데이터면 고칠 때마다
릴리스가 필요하고, 릴리스를 건너뛴 호스트는 영영 옛 세계를 본다. 사람의 장면은 데이터
루트에 두고 이 모듈은 **파일 경로를 모른다** — dict 를 받아 굽기만 한다.

## 기본 장면

`assets/default_scene.json` 이 지금의 큐브+통이다. 바탕 XML 에서 그 둘을 빼고 이리로
옮겼다 — 그래야 기본 장면의 물체도 사람이 고칠 수 있다. **완료 조건은 "지금과 똑같이
나온다"** 이고, 그 대조는 `backend/tests/test_sim_scene_spec.py` 가 수치로 한다.
"""

import json
import re
from pathlib import Path

ASSETS = Path(__file__).resolve().parent / "assets"
SCENE_XML = ASSETS / "piper_scene.xml"
DEFAULT_SCENE = ASSETS / "default_scene.json"

SPEC_VERSION = 1
#: 물체 수 상한 — 실시간 500Hz 를 지켜야 한다. 프리미티브는 여유가 크지만(실측: 물체
#: 셋에 물리 1초 15ms) 메시가 들어오면 이야기가 달라지므로 상한을 둔다.
MAX_OBJECTS = 32
ID_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")

#: 프리미티브 → (MuJoCo geom 이름, size 길이). MuJoCo 의 size 뜻은 **반지름·반변**이다.
PRIMITIVES = {
    "box": ("mjGEOM_BOX", 3),            # 반변 x·y·z
    "sphere": ("mjGEOM_SPHERE", 1),      # 반지름
    "cylinder": ("mjGEOM_CYLINDER", 2),  # 반지름, 반높이
    "capsule": ("mjGEOM_CAPSULE", 2),
    "ellipsoid": ("mjGEOM_ELLIPSOID", 3),
}

#: 바탕이 이미 쓰는 이름 — 물체 id 로 쓰면 모델이 깨진다. MuJoCo 도 중복 이름을 거절하지만
#: 그 오류는 사람이 못 읽는다("Error: repeated name"). 여기서 먼저 잡아 무엇이 문제인지 말한다.
RESERVED_IDS = frozenset({
    "table", "top", "front", "wrist", "sun", "fill",
    "piper_base", "gripper_base", "finger_l", "finger_r",
    *(f"link{i}" for i in range(1, 7)),
    *(f"joint{i}" for i in range(1, 7)),
    "gripper_l", "gripper_r", "world", "worldbody",
})

#: 움직이는 물체의 접촉 기본값 — 2026-09-14 비스듬한 파지 실험에서 얻은 값 그대로다
#: (`cone="elliptic"` 과 함께여야 뜻이 있다; 바탕 XML 의 `<option>` 이 그걸 쥔다).
#: condim 6 은 비틀림·구름 마찰 — 핀치에서 돌아 빠지는 것을 막는다.
MOVABLE_PHYSICS = {"friction": [2.0, 0.1, 0.001], "condim": 6, "solref": [0.005, 1.0]}
#: 고정물은 MuJoCo 기본값 그대로 둔다 — 잡을 물건이 아니라 부딪칠 물건이다.
#: (지금 통이 정확히 이 값으로 서 있다: condim 3, friction 1/0.005/1e-4, solref 0.02/1)
STATIC_PHYSICS = {"friction": [1.0, 0.005, 0.0001], "condim": 3, "solref": [0.02, 1.0]}


class SceneError(ValueError):
    """장면 JSON 이 규칙을 어겼다 — 메시지는 **사람이 고칠 수 있게** 쓴다."""


def _num(v, where: str) -> float:
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        raise SceneError(f"{where}: 숫자가 아닙니다 ({v!r})")
    f = float(v)
    if f != f or f in (float("inf"), float("-inf")):
        raise SceneError(f"{where}: 숫자가 아닙니다 ({v!r})")
    return f


def _vec(v, n: int, where: str, lo: float, hi: float) -> list[float]:
    if not isinstance(v, (list, tuple)) or len(v) != n:
        raise SceneError(f"{where}: 숫자 {n} 개가 필요합니다 ({v!r})")
    out = [_num(x, where) for x in v]
    for x in out:
        if not lo <= x <= hi:
            raise SceneError(f"{where}: {lo} ~ {hi} 범위를 벗어났습니다 ({x})")
    return out


def _bin_geoms(params: dict) -> list[dict]:
    """통 — 바닥 하나 + 벽 넷. **메시로는 못 만든다.**

    ⚠ MuJoCo 는 메시를 **볼록껍질**로 충돌시킨다 — 오목한 그릇을 메시 하나로 올리면
    겉만 오목하고 물리는 덩어리다(실측: V 자 골짜기에 떨어뜨린 공이 골짜기 바닥 0.010 이
    아니라 껍질 위 0.1096 에 섰다). 그래서 오목한 것은 볼록 조각으로 짓는다.
    """
    ix, iy = params["inner"]
    t, h = params["wall_t"], params["wall_h"]
    return [
        {"type": "box", "size": [ix, iy, params["floor_t"]], "pos": [0, 0, params["floor_t"]]},
        {"type": "box", "size": [t, iy, h], "pos": [ix, 0, h]},
        {"type": "box", "size": [t, iy, h], "pos": [-ix, 0, h]},
        {"type": "box", "size": [ix, t, h], "pos": [0, iy, h]},
        {"type": "box", "size": [ix, t, h], "pos": [0, -iy, h]},
    ]


#: 조립 프리셋 — 이름 → (기본 params, geom 생성기). 프리미티브로 지어야만 오목한 것이
#: 물리에서도 오목하다(위 주석). 사람이 툴 없이 쓸 수 있는 유일한 오목 경로다.
PRESETS = {
    "bin": ({"inner": [0.08, 0.08], "wall_t": 0.003, "wall_h": 0.03, "floor_t": 0.003},
            _bin_geoms),
}


def _validate_object(raw: dict, seen: set[str]) -> dict:
    if not isinstance(raw, dict):
        raise SceneError(f"물체가 객체가 아닙니다: {raw!r}")
    oid = raw.get("id")
    if not isinstance(oid, str) or not ID_RE.match(oid):
        raise SceneError(f"id 는 소문자로 시작하는 영숫자·_·- 32자 이내여야 합니다: {oid!r}")
    if oid in RESERVED_IDS:
        raise SceneError(f"id '{oid}' 는 팔·테이블·카메라가 쓰는 이름입니다 — 다른 이름을 쓰세요")
    if oid in seen:
        raise SceneError(f"id '{oid}' 가 둘입니다 — 장면 안에서 id 는 유일해야 합니다")
    seen.add(oid)

    shape = raw.get("shape")
    if not isinstance(shape, str):
        raise SceneError(f"{oid}: shape 가 없습니다")
    movable = raw.get("movable", True)
    if not isinstance(movable, bool):
        raise SceneError(f"{oid}: movable 은 true/false 여야 합니다")

    obj = {
        "id": oid,
        "label": str(raw.get("label") or oid),
        "shape": shape,
        "movable": movable,
        "pos": _vec(raw.get("pos", [0, 0, 0]), 3, f"{oid}.pos", -2.0, 2.0),
        "euler_deg": _vec(raw.get("euler_deg", [0, 0, 0]), 3, f"{oid}.euler_deg", -360.0, 360.0),
        "rgba": _vec(raw.get("rgba", [0.7, 0.7, 0.72, 1.0]), 4, f"{oid}.rgba", 0.0, 1.0),
    }

    if shape.startswith("preset:"):
        name = shape.split(":", 1)[1]
        if name not in PRESETS:
            raise SceneError(f"{oid}: 모르는 프리셋 '{name}' — 있는 것: "
                             + ", ".join(sorted(PRESETS)))
        defaults, _ = PRESETS[name]
        params = dict(defaults)
        for k, v in (raw.get("params") or {}).items():
            if k not in defaults:
                raise SceneError(f"{oid}: 프리셋 '{name}' 에 없는 항목 '{k}' — 있는 것: "
                                 + ", ".join(sorted(defaults)))
            params[k] = (_vec(v, len(defaults[k]), f"{oid}.params.{k}", 1e-4, 1.0)
                         if isinstance(defaults[k], list) else _num(v, f"{oid}.params.{k}"))
        for k, v in params.items():
            if not isinstance(v, list) and not 1e-4 <= v <= 1.0:
                raise SceneError(f"{oid}.params.{k}: 0.0001 ~ 1 m 여야 합니다 ({v})")
        obj["params"] = params
    elif shape in PRIMITIVES:
        n = PRIMITIVES[shape][1]
        obj["size"] = _vec(raw.get("size"), n, f"{oid}.size", 1e-4, 1.0)
    elif shape == "mesh":
        # 크기는 자산의 `unit_scale`(사람이 확인한 단위)이 쥔다 — 여기 `scale` 은 그 위에
        # 얹는 배율이다. ⚠ 자산 **파일 자체는 명세에 안 실린다**: 몇 MB 를 버스로 못 보낸다.
        #   id 만 싣고 양쪽이 자기 루트에서 푼다 (piper_sim/assets.py 의 표).
        asset = raw.get("asset")
        if not isinstance(asset, str) or not re.fullmatch(r"[0-9a-f]{16}", asset):
            raise SceneError(f"{oid}: mesh 는 asset(자산 id)이 필요합니다 ({asset!r})")
        obj["asset"] = asset
        obj["scale"] = _num(raw.get("scale", 1.0), f"{oid}.scale")
        if not 1e-3 <= obj["scale"] <= 1e3:
            raise SceneError(f"{oid}.scale: 0.001 ~ 1000 이어야 합니다 ({obj['scale']})")
    else:
        raise SceneError(f"{oid}: 모르는 shape '{shape}' — 있는 것: "
                         + ", ".join(sorted(PRIMITIVES) + ["mesh"]
                                     + [f"preset:{p}" for p in sorted(PRESETS)]))

    base = MOVABLE_PHYSICS if movable else STATIC_PHYSICS
    obj["friction"] = _vec(raw.get("friction", base["friction"]), 3, f"{oid}.friction", 0.0, 100.0)
    obj["solref"] = _vec(raw.get("solref", base["solref"]), 2, f"{oid}.solref", -1e4, 1e4)
    condim = raw.get("condim", base["condim"])
    if condim not in (1, 3, 4, 6):
        raise SceneError(f"{oid}: condim 은 1·3·4·6 중 하나여야 합니다 ({condim!r})")
    obj["condim"] = int(condim)
    if movable:
        mass = _num(raw.get("mass", 0.05), f"{oid}.mass")
        if not 1e-4 <= mass <= 50.0:
            raise SceneError(f"{oid}.mass: 0.0001 ~ 50 kg 여야 합니다 ({mass})")
        obj["mass"] = mass
    return obj


def validate(spec: dict) -> dict:
    """장면 dict 를 검사하고 **기본값을 채운 사본**을 돌려준다.

    화면도 데몬도 이 결과를 쓴다 — 기본값이 두 곳에서 갈리면 "화면엔 이렇게 보이는데
    시뮬은 저렇게 돈다"가 된다.
    """
    if not isinstance(spec, dict):
        raise SceneError("장면이 JSON 객체가 아닙니다")
    version = spec.get("version", SPEC_VERSION)
    if version != SPEC_VERSION:
        raise SceneError(f"모르는 장면 버전입니다: {version} (이 프로그램은 {SPEC_VERSION})")
    objects = spec.get("objects", [])
    if not isinstance(objects, list):
        raise SceneError("objects 가 배열이 아닙니다")
    if len(objects) > MAX_OBJECTS:
        raise SceneError(f"물체가 {len(objects)} 개입니다 — 상한은 {MAX_OBJECTS} 개입니다")
    seen: set[str] = set()
    return {
        "version": SPEC_VERSION,
        "id": str(spec.get("id") or "scene"),
        "name": str(spec.get("name") or "이름 없는 장면"),
        "objects": [_validate_object(o, seen) for o in objects],
    }


def load(path: Path | str) -> dict:
    """파일에서 장면을 읽어 검사한다 — 웹의 "환경 불러오기"가 타는 길."""
    p = Path(path)
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise SceneError(f"장면 파일이 없습니다: {p}")
    except json.JSONDecodeError as exc:
        raise SceneError(f"장면 JSON 을 읽을 수 없습니다 ({p.name} {exc.lineno}번째 줄): {exc.msg}")
    return validate(raw)


def default() -> dict:
    return load(DEFAULT_SCENE)


def rest_z(obj: dict) -> float:
    """이 물체를 테이블(윗면 z=0)에 **앉혔을 때** body 원점이 있어야 할 높이.

    배치(클릭·드래그)는 x·y 만 받는다 — z 를 사람에게 묻는 UI 는 쓸 수 없다. 모양마다
    반높이가 달라서 상수 하나로는 안 되고(예전 코드는 큐브 반변 0.02 가 박혀 있었다),
    구를 그 높이에 놓으면 파묻히거나 뜬다.
    """
    if obj["shape"].startswith("preset:"):
        return 0.0                      # 프리셋은 바닥이 원점이다(_bin_geoms 가 그렇게 짓는다)
    if obj["shape"] == "mesh":
        # 메시도 0 이다 — 올릴 때 AABB 아래면 가운데를 원점으로 옮겨 뒀다(assets.add).
        # 실측: geom pos = origin_offset×scale 로 두면 body 가 테이블 z=0 에 앉는다.
        return 0.0
    size, shape = obj["size"], obj["shape"]
    if shape == "sphere":
        return size[0]
    if shape == "cylinder":
        return size[1]                  # 반높이
    if shape == "capsule":
        return size[1] + size[0]        # 반높이 + 반구
    return size[2]                      # box·ellipsoid 는 z 반변


def _geoms_of(obj: dict) -> list[dict]:
    if obj["shape"].startswith("preset:"):
        return PRESETS[obj["shape"].split(":", 1)[1]][1](obj["params"])
    return [{"type": obj["shape"], "size": obj["size"], "pos": [0, 0, 0]}]


def euler_quat(euler_deg: list[float]) -> list[float]:
    """오일러(도) → 쿼터니언. MuJoCo 의 기본 `eulerseq="xyz"`(내재)와 같은 뜻이 되게
    `mju_euler2Quat` 에 맡긴다 — 손으로 곱하면 순서를 틀리기 쉽다."""
    import math

    import mujoco
    import numpy as np

    q = np.zeros(4)
    mujoco.mju_euler2Quat(q, np.array([math.radians(a) for a in euler_deg]), "xyz")
    return [float(x) for x in q]


def compose(spec: dict, base: Path | str | None = None):
    """바탕 MJCF + 장면 물체 → `MjSpec`. 부르는 쪽이 `compile()` 한다.

    ⚠ 바탕을 **파일에서 다시 연다**. 한 번 연 `MjSpec` 을 재사용하면 장면을 갈아끼울
    때마다 옛 물체가 남는다 — 지우는 API 를 쓰는 것보다 다시 읽는 편이 싸고 확실하다.
    """
    import mujoco

    s = mujoco.MjSpec.from_file(str(base or SCENE_XML))
    for obj in validate(spec)["objects"]:
        body = s.worldbody.add_body(name=obj["id"], pos=obj["pos"], quat=euler_quat(obj["euler_deg"]))
        if obj["movable"]:
            # ⚠ 이름은 `<id>_free` 로 고정이다 — world.reset 이 이 이름으로 qpos 를 찾는다.
            body.add_freejoint(name=f"{obj['id']}_free")
        if obj["shape"] == "mesh":
            _add_mesh_geom(s, body, obj)
            continue
        geoms = _geoms_of(obj)
        for i, g in enumerate(geoms):
            kw = {}
            if obj["movable"]:
                # 여러 조각이면 질량을 나눠 준다 — 합이 사람이 적은 질량이 되게.
                kw["mass"] = obj["mass"] / len(geoms)
            body.add_geom(
                name=f"{obj['id']}_geom" if len(geoms) == 1 else f"{obj['id']}_geom{i}",
                type=getattr(mujoco.mjtGeom, PRIMITIVES[g["type"]][0]),
                size=list(g["size"]) + [0.0] * (3 - len(g["size"])),
                pos=list(g["pos"]), rgba=obj["rgba"],
                condim=obj["condim"], friction=obj["friction"], solref=obj["solref"], **kw)
    return s


def _add_mesh_geom(s, body, obj: dict) -> None:
    """메시 물체 하나 — 자산을 읽어 `<mesh>` 를 달고 geom 을 얹는다.

    ⚠ **MuJoCo 는 메시를 자기 무게중심으로 옮겨 놓고** geom 위치로 그걸 되돌린다. 그래서
    파일의 원점이 그대로 geom 프레임이 된다(실측). 우리는 그 위에 `origin_offset`(AABB
    아래면 가운데 → 원점)을 얹어, 놓으면 테이블에 앉게 한다 — 실측: body z 가 -0.0002 로
    안착하고 geom 중심이 (0, 0, 반높이)에 온다.

    ⚠ 충돌은 **볼록껍질**이다. 오목한 물건은 겉만 오목하고 물리는 덩어리다 — 올릴 때 재서
    (`mesh.concavity`) 화면이 경고한다. 그릇·통은 `preset:bin` 으로 짓는 편이 낫다.
    """
    import mujoco

    from piper_sim import assets

    try:
        m = assets.meta(obj["asset"])
        path = assets.mesh_path(obj["asset"])
    except assets.AssetError as exc:
        raise SceneError(f"{obj['id']}: {exc}")
    total = float(m.get("unit_scale") or 1.0) * float(obj["scale"])
    name = f"{obj['id']}_mesh"
    s.add_mesh(name=name, file=str(path), scale=[total] * 3)
    off = [v * total for v in (m["raw"]["origin_offset"])]
    body.add_geom(name=f"{obj['id']}_geom", type=mujoco.mjtGeom.mjGEOM_MESH, meshname=name,
                  pos=off, rgba=obj["rgba"], condim=obj["condim"], friction=obj["friction"],
                  solref=obj["solref"], **({"mass": obj["mass"]} if obj["movable"] else {}))


def build(spec: dict | None = None, base: Path | str | None = None):
    """장면 → 컴파일된 `MjModel`. `spec=None` 이면 기본 장면."""
    if not Path(base or SCENE_XML).exists():
        raise SceneError(f"바탕 씬이 없습니다: {base or SCENE_XML}"
                         " — python3 tools/build_sim_scene.py 로 구우세요")
    return compose(default() if spec is None else spec, base).compile()


def describe(spec: dict) -> dict:
    """화면이 읽을 요약 — 굽지 않고도 무엇이 들었는지 말한다."""
    v = validate(spec)
    return {"id": v["id"], "name": v["name"], "count": len(v["objects"]),
            "objects": [{"id": o["id"], "label": o["label"], "shape": o["shape"],
                         "movable": o["movable"], "pos": o["pos"]} for o in v["objects"]]}


__all__ = ["SceneError", "SPEC_VERSION", "MAX_OBJECTS", "PRIMITIVES", "PRESETS",
           "RESERVED_IDS", "SCENE_XML", "DEFAULT_SCENE", "validate", "load", "default",
           "compose", "build", "describe", "rest_z", "euler_quat"]
