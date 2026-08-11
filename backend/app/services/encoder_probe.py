"""이미지 엔코더 프로브 세션 관리 + 특징 분석.

wrapper/encoder_probe.py 가 1회성 subprocess 로 저장한 (N, D) 패치 특징을 numpy 로만
다룬다. 백엔드에 torch/lerobot 을 들이지 않기 위한 분리이며(CLI 래핑 원칙), 클릭 유사도 /
PCA / k-means 는 모델 재실행 없이 캐시된 특징에서 즉시 계산된다.

두 이미지를 비교할 때는 기준(ref) 세션에서 PCA 기저와 정규화 범위를 잡아 양쪽에 똑같이
적용한다. 각자 따로 PCA 를 돌리면 색이 달라져 비교 자체가 성립하지 않는다.
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from app.core.config import settings

logger = logging.getLogger(__name__)

PROBE_SCRIPT = str(Path(__file__).resolve().parents[3] / "wrapper" / "encoder_probe.py")
MAX_SESSIONS = 8
PROBE_TIMEOUT = 300.0


def _probe_python() -> str:
    """lerobot 이 설치된 인터프리터. 절대경로가 없으면 PATH 의 python 으로 폴백."""
    if Path(settings.grpc_python).exists():
        return settings.grpc_python
    return settings.local_python


def _base_dir() -> Path:
    shm = Path("/dev/shm")
    root = shm / "piper_encoder" if shm.is_dir() else Path(tempfile.gettempdir()) / "piper_encoder"
    root.mkdir(parents=True, exist_ok=True)
    return root


@dataclass
class ProbeSession:
    sid: str
    path: Path
    meta: dict
    created: float = field(default_factory=time.time)
    _feat: np.ndarray | None = None

    def features(self) -> np.ndarray:
        if self._feat is None:
            self._feat = np.load(self.path / "features.npy").astype(np.float32)
        return self._feat

    def valid_mask(self) -> np.ndarray:
        """패딩에 걸친 패치를 제외한 마스크.

        SmolVLA 는 위/왼쪽에만 패딩을 넣으므로(resize_with_pad) 그 영역 패치는 내용이 없다.
        PCA 기저나 k-means 중심을 여기서 잡으면 검은 패딩이 한 클러스터를 통째로 차지한다.
        """
        gh, gw = self.meta["grid_h"], self.meta["grid_w"]
        r0, c0 = self.meta.get("valid_row0", 0), self.meta.get("valid_col0", 0)
        mask = np.zeros((gh, gw), dtype=bool)
        mask[r0:, c0:] = True
        return mask.reshape(-1)

    def to_dict(self) -> dict:
        return {"sid": self.sid, "meta": self.meta, "created": self.created}


class EncoderProbeManager:
    def __init__(self) -> None:
        self._sessions: dict[str, ProbeSession] = {}

    # ── 세션 ──

    def get(self, sid: str) -> ProbeSession | None:
        return self._sessions.get(sid)

    def list(self) -> list[dict]:
        return [s.to_dict() for s in sorted(self._sessions.values(), key=lambda s: s.created)]

    def delete(self, sid: str) -> bool:
        sess = self._sessions.pop(sid, None)
        if not sess:
            return False
        shutil.rmtree(sess.path, ignore_errors=True)
        return True

    def _evict(self) -> None:
        while len(self._sessions) > MAX_SESSIONS:
            oldest = min(self._sessions.values(), key=lambda s: s.created)
            self.delete(oldest.sid)

    # ── 인코딩 ──

    def run(
        self,
        image_bytes: bytes,
        policy_type: str,
        checkpoint: str = "",
        image_key: str = "",
        tap: str = "siglip",
        device: str = "cuda",
    ) -> ProbeSession:
        """subprocess 로 엔코더를 1회 실행하고 세션을 만든다 (블로킹)."""
        sid = uuid.uuid4().hex[:12]
        out = _base_dir() / sid
        out.mkdir(parents=True, exist_ok=True)
        src = out / "source.jpg"
        src.write_bytes(image_bytes)

        cmd = [
            _probe_python(), "-u", PROBE_SCRIPT,
            "--policy-type", policy_type,
            "--image", str(src),
            "--out", str(out),
            "--device", device,
            "--tap", tap,
        ]
        if checkpoint:
            cmd += ["--checkpoint", checkpoint]
        if image_key:
            cmd += ["--image-key", image_key]

        logger.info("encoder_probe: %s", " ".join(cmd))
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=PROBE_TIMEOUT)
        except subprocess.TimeoutExpired:
            shutil.rmtree(out, ignore_errors=True)
            raise RuntimeError(f"엔코더 실행이 {PROBE_TIMEOUT:.0f}초를 초과했습니다")

        if proc.returncode != 0:
            shutil.rmtree(out, ignore_errors=True)
            tail = (proc.stderr or proc.stdout or "").strip().splitlines()
            raise RuntimeError(tail[-1] if tail else f"엔코더 실행 실패 (code {proc.returncode})")

        meta_path = out / "meta.json"
        if not meta_path.exists():
            shutil.rmtree(out, ignore_errors=True)
            raise RuntimeError("엔코더가 결과를 남기지 않았습니다")

        meta = json.loads(meta_path.read_text())
        sess = ProbeSession(sid=sid, path=out, meta=meta)
        meta["feature_stats"] = feature_stats(sess)
        self._sessions[sid] = sess
        self._evict()
        return sess


# ── 특징 분석 (numpy) ──────────────────────────────────────────────────────────


def _l2norm(x: np.ndarray) -> np.ndarray:
    return x / (np.linalg.norm(x, axis=-1, keepdims=True) + 1e-8)


def pca_rgb(target: ProbeSession, ref: ProbeSession) -> dict:
    """ref 의 유효 패치로 PCA 기저를 잡아 target 에 적용하고 RGB 로 매핑한다.

    특징을 L2 정규화한 뒤 분해한다. SigLIP 계열은 소수의 high-norm 아웃라이어 토큰이
    존재해(이 장면에서 최대/중앙값 5배) 원본 크기 그대로 PCA를 돌리면 그 토큰들이 기저를
    독점하고 결과가 노이즈처럼 보인다. 방향만 남기면 클릭 유사도(코사인)와도 기준이 같아진다.
    """
    ref_feat = _l2norm(ref.features())[ref.valid_mask()]
    mu = ref_feat.mean(axis=0)
    centered = ref_feat - mu
    _, sv, vt = np.linalg.svd(centered, full_matrices=False)
    basis = vt[:3].T  # (D, 3)

    ref_proj = centered @ basis
    lo = np.percentile(ref_proj, 2, axis=0)
    hi = np.percentile(ref_proj, 98, axis=0)

    proj = (_l2norm(target.features()) - mu) @ basis
    rgb = np.clip((proj - lo) / (hi - lo + 1e-8), 0, 1)

    total = float((sv ** 2).sum()) or 1.0
    return {
        "rgb": (rgb * 255).astype(np.uint8).tolist(),
        "explained": [round(float(s ** 2 / total), 4) for s in sv[:3]],
    }


def cosine_map(target: ProbeSession, ref: ProbeSession, patch: int) -> dict:
    """ref 의 patch 번째 토큰과 target 전체 토큰의 코사인 유사도."""
    ref_feat = ref.features()
    if not 0 <= patch < ref_feat.shape[0]:
        raise ValueError(f"패치 인덱스 범위를 벗어났습니다: {patch}")
    query = _l2norm(ref_feat[patch])
    values = _l2norm(target.features()) @ query
    mask = target.valid_mask()
    valid = values[mask]
    out = {
        "values": [round(float(v), 4) for v in values],
        "min": round(float(valid.min()), 4),
        "max": round(float(valid.max()), 4),
        "mean": round(float(valid.mean()), 4),
    }
    if target is ref:
        out.update(_locality(target, values, patch, mask))
    return out


def _locality(sess: ProbeSession, values: np.ndarray, patch: int, mask: np.ndarray, top: int = 20) -> dict:
    """가장 비슷한 패치들이 질의 지점 주변에 모이는지 — 눈짐작을 대신하는 수치.

    상위 top개 매칭의 평균 격자 거리를, "아무 곳이나 골랐을 때의 평균 거리"(baseline)와
    비교한다. 비율이 낮을수록 그 물체에만 반응한다는 뜻이고, 1에 가까우면 장면 전체가
    비슷하게 보인다는 뜻이다(= 특징이 물체를 구분하지 못함).
    """
    gw = sess.meta["grid_w"]
    rows, cols = np.divmod(np.arange(values.shape[0]), gw)
    qr, qc = divmod(patch, gw)
    dist = np.hypot(rows - qr, cols - qc)

    scored = np.where(mask, values, -np.inf)
    scored[patch] = -np.inf  # 질의 자신은 제외 (항상 1.0)
    order = np.argsort(scored)[::-1][:top]
    baseline = float(dist[mask].mean()) or 1.0
    locality = float(dist[order].mean())
    return {
        "locality": round(locality, 2),
        "locality_baseline": round(baseline, 2),
        "locality_ratio": round(locality / baseline, 3),
    }


def feature_stats(sess: ProbeSession) -> dict:
    """패치 특징 크기 분포. SigLIP 계열은 소수 토큰의 norm 이 유독 커서(아웃라이어 토큰)
    PCA 가 얼룩덜룩해 보이는데, 그 정도를 수치로 남긴다."""
    norms = np.linalg.norm(sess.features()[sess.valid_mask()], axis=1)
    median = float(np.median(norms)) or 1.0
    return {
        "norm_median": round(median, 2),
        "norm_max": round(float(norms.max()), 2),
        "norm_outlier_ratio": round(float(norms.max()) / median, 2),
    }


def kmeans_labels(target: ProbeSession, k: int, seed: int = 0) -> dict:
    """유효 패치를 코사인 기준 k개로 군집화. 패딩 패치는 -1."""
    mask = target.valid_mask()
    x = _l2norm(target.features()[mask])
    n = x.shape[0]
    k = max(2, min(int(k), 12, n))

    rng = np.random.default_rng(seed)
    # k-means++ 초기화
    centers = [x[rng.integers(n)]]
    for _ in range(k - 1):
        d = 1.0 - np.max(x @ np.stack(centers).T, axis=1)
        d = np.clip(d, 0, None)
        total = d.sum()
        idx = int(rng.integers(n)) if total <= 0 else int(rng.choice(n, p=d / total))
        centers.append(x[idx])
    centers = np.stack(centers)

    labels = np.zeros(n, dtype=np.int32)
    for _ in range(30):
        new = np.argmax(x @ centers.T, axis=1).astype(np.int32)
        if np.array_equal(new, labels):
            break
        labels = new
        for j in range(k):
            sel = x[labels == j]
            if len(sel):
                centers[j] = sel.mean(axis=0) / (np.linalg.norm(sel.mean(axis=0)) + 1e-8)

    out = np.full(mask.shape[0], -1, dtype=np.int32)
    out[mask] = labels
    return {"labels": out.tolist(), "k": k}


encoder_probe_manager = EncoderProbeManager()
