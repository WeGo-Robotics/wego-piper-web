"""메시 읽기와 재기 — 사람이 올린 파일을 받기 전에 **무엇인지 말해 준다**
(feature/sim-scene-editor.md §2·§4).

## 왜 여기서 직접 읽나

의존성을 안 늘린다. OBJ 와 바이너리 STL 은 둘 다 단순해서 numpy 로 충분하고, 게이트웨이
이미지는 `piper_sim` 을 `--no-deps` 로 깔기 때문에(mujoco 없음) **어차피 MuJoCo 로는 못 잰다.**
trimesh·open3d 를 들이면 이미지가 커지고, 얻는 것은 여기 있는 몇십 줄뿐이다.

## MuJoCo 가 먹는 것 (이 기계의 3.12.0 으로 실측)

| 넣어 본 것 | 결과 |
|---|---|
| `.obj` | ✅ |
| `.stl` **바이너리** | ✅ |
| `.stl` **ASCII** | ❌ `stl_decoder: number of faces should be between 1 and 200000` |
| `.gltf` · `.glb` · `.dae` · `.ply` | ❌ |
| 텍스처 `.png` | ✅ / `.jpg` ❌ |

ASCII STL 과 GLB 는 **흔한 내보내기 기본값**이다. 거절만 하면 "왜 안 되는지 모르겠다"가
반드시 나오므로 무엇으로 바꿔 오면 되는지까지 말한다.

## 재는 것 셋 — 스캔·CAD 가 전부 걸리는 자리

1. **크기** — OBJ·STL 에는 단위가 없다. mm 로 뜬 모델을 그대로 쓰면 1000배로 선다.
   시스템이 알아낼 방법이 없으니 **사람에게 보여 주고 묻는다**.
2. **바닥** — 스캔은 원점이 엉뚱하다. AABB 아래면 가운데를 물체 원점으로 옮겨, 놓으면
   테이블에 앉게 한다.
3. **오목함** — ⚠ MuJoCo 는 메시를 **볼록껍질로 충돌시킨다**(실측: V 자 골짜기에 떨어뜨린
   공이 골짜기 바닥 0.010 이 아니라 껍질 위 0.1096 에 섰다). 그릇·컵은 겉만 오목하고 물리는
   덩어리가 된다. 얼마나 파였는지를 **재서** 말해 준다.
"""

import struct
from pathlib import Path

import numpy as np

#: MuJoCo 가 여는 메시 확장자 (실측). 다른 것은 받지 않는다.
MESH_EXT = (".obj", ".stl")
TEXTURE_EXT = (".png",)
#: 받지 않는 흔한 포맷 → 무엇으로 바꿔 오면 되는지
CONVERT_HINT = {
    ".glb": "Blender 로 열어 OBJ 또는 **바이너리** STL 로 내보내세요",
    ".gltf": "Blender 로 열어 OBJ 또는 **바이너리** STL 로 내보내세요",
    ".dae": "Blender 로 열어 OBJ 로 내보내세요",
    ".fbx": "Blender 로 열어 OBJ 로 내보내세요",
    ".ply": "Blender 로 열어 OBJ 로 내보내세요",
    ".step": "FreeCAD 로 열어 **바이너리** STL 로 내보내세요",
    ".stp": "FreeCAD 로 열어 **바이너리** STL 로 내보내세요",
    ".jpg": "텍스처는 PNG 만 됩니다 — 이미지 편집기로 PNG 로 저장하세요",
    ".jpeg": "텍스처는 PNG 만 됩니다 — 이미지 편집기로 PNG 로 저장하세요",
}
#: 면이 이보다 많으면 볼록 판정에서 표본만 본다 (V×F 가 통째로 메모리에 안 들어간다)
CONVEX_FACE_BUDGET = 20_000


class MeshError(ValueError):
    """사람이 고칠 수 있는 실패 — 무엇으로 바꿔 오면 되는지까지 적는다."""


def _obj(text: str) -> tuple[np.ndarray, np.ndarray]:
    verts: list[tuple[float, float, float]] = []
    faces: list[tuple[int, int, int]] = []
    for line in text.splitlines():
        if line.startswith("v "):
            p = line.split()
            verts.append((float(p[1]), float(p[2]), float(p[3])))
        elif line.startswith("f "):
            # `f a/b/c` 도 `f a//c` 도 `f a` 도 온다 — 첫 숫자만 쓴다. 음수는 뒤에서 센다.
            idx = []
            for tok in line.split()[1:]:
                n = int(tok.split("/")[0])
                idx.append(n - 1 if n > 0 else len(verts) + n)
            for i in range(1, len(idx) - 1):        # 다각형은 부채꼴로 쪼갠다
                faces.append((idx[0], idx[i], idx[i + 1]))
    if not verts or not faces:
        raise MeshError("OBJ 에서 꼭짓점이나 면을 못 찾았습니다 — 빈 파일이거나 다른 포맷입니다")
    return np.asarray(verts, float), np.asarray(faces, np.int64)


def _stl(raw: bytes) -> tuple[np.ndarray, np.ndarray]:
    # ⚠ ASCII STL 은 MuJoCo 가 못 읽는다 — 여기서 잡아 무엇으로 바꿀지 말한다.
    #   (MuJoCo 는 "number of faces should be between 1 and 200000" 이라고만 한다.)
    head = raw[:256].lstrip()
    if head[:5].lower() == b"solid" and b"facet normal" in raw[:2048]:
        raise MeshError("ASCII STL 입니다 — MuJoCo 는 **바이너리** STL 만 읽습니다. "
                        "Blender 의 STL 내보내기는 기본이 바이너리입니다")
    if len(raw) < 84:
        raise MeshError("STL 이 너무 짧습니다 — 파일이 깨졌습니다")
    n = struct.unpack("<I", raw[80:84])[0]
    if len(raw) < 84 + 50 * n:
        raise MeshError(f"STL 면 수({n})와 파일 크기가 안 맞습니다 — 파일이 깨졌습니다")
    buf = np.frombuffer(raw[84:84 + 50 * n], dtype=np.uint8).reshape(n, 50)
    tri = buf[:, 12:48].copy().view(np.float32).reshape(n, 3, 3).astype(float)
    verts = tri.reshape(-1, 3)
    faces = np.arange(n * 3, dtype=np.int64).reshape(n, 3)
    return verts, faces


def weld(verts: np.ndarray, faces: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """같은 자리의 꼭짓점을 하나로 — **재기 전에 반드시** 한다.

    ⚠ STL 에는 꼭짓점 공유가 없다. 삼각형마다 좌표 셋을 새로 적으므로 모서리가 한 면에만
    붙은 것처럼 보이고, `is_closed` 가 멀쩡한 솔리드를 전부 "열림"이라고 답한다(실측: 닫힌
    정사면체 STL 이 열림으로 나왔다). 스캔 OBJ 도 중복 꼭짓점이 흔하다.

    허용오차는 바운딩 박스 대각선에 비례한다 — 절대값으로 두면 작은 모델에서 멀쩡한
    꼭짓점까지 붙는다.
    """
    if len(verts) == 0:
        return verts, faces
    diag = float(np.linalg.norm(verts.max(axis=0) - verts.min(axis=0))) or 1.0
    q = np.round(verts / (diag * 1e-6)).astype(np.int64)
    _, first, inv = np.unique(q, axis=0, return_index=True, return_inverse=True)
    out_v = verts[first]
    out_f = inv[faces]
    ok = (out_f[:, 0] != out_f[:, 1]) & (out_f[:, 1] != out_f[:, 2]) & (out_f[:, 0] != out_f[:, 2])
    return out_v, out_f[ok]


def read(path: Path | str) -> tuple[np.ndarray, np.ndarray]:
    """파일 → (꼭짓점 N×3, 삼각형 M×3). 못 읽으면 **무엇으로 바꿔 오면 되는지** 말한다."""
    p = Path(path)
    ext = p.suffix.lower()
    if ext in CONVERT_HINT:
        raise MeshError(f"{ext} 는 MuJoCo 가 못 읽습니다 — {CONVERT_HINT[ext]}")
    if ext not in MESH_EXT:
        raise MeshError(f"{ext or '확장자 없음'} 는 받지 않습니다 — OBJ 또는 바이너리 STL 을 주세요")
    raw = p.read_bytes()
    v, f = _obj(raw.decode("utf-8", "replace")) if ext == ".obj" else _stl(raw)
    return weld(v, f)


def _volume(verts: np.ndarray, faces: np.ndarray) -> float:
    """부호 있는 부피 (발산정리). 면 방향이 뒤집혔으면 음수라 절댓값을 쓴다."""
    a, b, c = verts[faces[:, 0]], verts[faces[:, 1]], verts[faces[:, 2]]
    return float(abs(np.einsum("ij,ij->i", a, np.cross(b, c)).sum()) / 6.0)


def is_closed(faces: np.ndarray) -> bool:
    """닫힌 껍데기인가 — 모든 모서리가 정확히 두 면에 붙어 있나.

    스캔은 바닥이 뚫린 채 오기 일쑤다. 열린 메시는 **부피도 오목함도 뜻이 흐려지므로**
    (안팎이 안 정해진다) 그 사실을 같이 말해 준다.
    """
    if len(faces) == 0:
        return False
    e = np.sort(np.concatenate([faces[:, [0, 1]], faces[:, [1, 2]], faces[:, [2, 0]]]), axis=1)
    _, counts = np.unique(e, axis=0, return_counts=True)
    return bool((counts == 2).all())


def concavity(verts: np.ndarray, faces: np.ndarray) -> float:
    """**껍질보다 얼마나 파였나** (m). 0 이면 볼록하다.

    면마다 그 평면을 긋고 모든 꼭짓점의 부호 거리를 잰다. 볼록체는 어떤 면 평면에 대해서도
    꼭짓점이 **한쪽에만** 있다. 양쪽에 걸쳐 있으면 그 얇은 쪽의 두께가 파인 깊이다.

    ⚠ **면 방향(winding)에 기대지 않는다.** 처음엔 "평면 바깥으로 나간 최대 거리"로 쟀는데,
    그건 법선이 바깥을 향한다는 가정이라 뒤집힌 면 하나에 무너진다 — 실제로 시험용 V 자
    골짜기에서 깊이 0.1m 를 **0 으로** 답했다. 한쪽/양쪽만 보면 그 가정이 필요 없다.

    ⚠ 열린 메시(`is_closed` 가 False)는 이 값이 0 이어도 볼록하다는 뜻이 아니다 — 안팎이
    안 정해져서다. 화면은 둘을 같이 보여 준다.

    ⚠ V×F 행렬은 큰 메시에서 통째로 안 들어간다(2만 정점 × 4만 면 = 6GB). 면을 블록으로
    끊어 돌고, 예산을 넘으면 면을 **고르게 표본**한다 — 오목함은 국소적이지 않아서 표본으로도
    잡힌다(컵의 안쪽은 면 하나가 아니라 수천 개다).
    """
    if len(faces) == 0:
        return 0.0
    f = faces
    if len(f) > CONVEX_FACE_BUDGET:
        f = f[np.linspace(0, len(f) - 1, CONVEX_FACE_BUDGET).astype(np.int64)]
    a, b, c = verts[f[:, 0]], verts[f[:, 1]], verts[f[:, 2]]
    nrm = np.cross(b - a, c - a)
    ln = np.linalg.norm(nrm, axis=1)
    keep = ln > 1e-12                       # 퇴화 삼각형은 평면이 없다
    if not keep.any():
        return 0.0
    nrm, a = nrm[keep] / ln[keep, None], a[keep]
    worst = 0.0
    for i in range(0, len(nrm), 512):       # 블록으로 끊어 메모리를 묶는다
        d = verts @ nrm[i:i + 512].T - np.einsum("ij,ij->i", a[i:i + 512], nrm[i:i + 512])
        thin = np.minimum(d.max(axis=0), -d.min(axis=0))   # 얇은 쪽 = 파인 깊이
        worst = max(worst, float(thin.max()))
    return max(0.0, worst)


def measure(verts: np.ndarray, faces: np.ndarray) -> dict:
    """올린 메시가 무엇인지 — 화면이 사람에게 보여 줄 값들. 단위는 **파일 그대로**다."""
    lo, hi = verts.min(axis=0), verts.max(axis=0)
    size = hi - lo
    return {
        "vertices": int(len(verts)),
        "faces": int(len(faces)),
        "bbox": [float(v) for v in size],
        "min": [float(v) for v in lo],
        "max": [float(v) for v in hi],
        "volume": _volume(verts, faces),
        # 바닥 가운데를 물체 원점으로 — 놓으면 테이블에 앉는다(스캔은 원점이 엉뚱하다)
        "origin_offset": [float(-(lo[0] + hi[0]) / 2), float(-(lo[1] + hi[1]) / 2), float(-lo[2])],
        "concavity": concavity(verts, faces),
        "closed": is_closed(faces),
    }


def inspect(path: Path | str) -> dict:
    v, f = read(path)
    return measure(v, f)
