"""조명 변화 감시 — 밝기·색이 튀면 알람 (feature/lighting-watch.md).

측정은 발행물이고 알람은 소비자 1호다. 판정은 EWMA 두 개의 괴리 + 3×3 격자
전역성 + 히스테리시스 — 전부 순수 함수라 하드웨어 없이 여기서 검증한다.
"""

import numpy as np
import pytest
from fastapi.testclient import TestClient

from piper_cam.lighting import Judge, JudgeConfig, features


def _frame(b: int, g: int, r: int, size: int = 120) -> np.ndarray:
    f = np.zeros((size, size, 3), dtype=np.uint8)
    f[:, :, 0], f[:, :, 1], f[:, :, 2] = b, g, r
    return f


def _feats(luma: float, grid: list[float] | None = None,
           rg: float = 0.0, bg: float = 0.0) -> dict:
    return {"luma": luma, "log_rg": rg, "log_bg": bg,
            "grid": grid if grid is not None else [luma] * 9}


def _run(judge: Judge, seq: list[dict], dt: float = 2.0) -> list[list[dict]]:
    """샘플 주기(2초)로 차례로 먹이고 판정 목록의 흐름을 돌려준다."""
    return [judge.update(f, i * dt) for i, f in enumerate(seq)]


def test_the_input_is_bgr_and_the_color_sign_proves_it():
    """⚠ **세그먼트는 BGR 이다** (rsd 는 to_bgr 후 발행, camerad 는 OpenCV 그대로 —
    yolod 의 "RGB" 주석이 틀린 것이다). R/B 가 뒤집히면 색 변화의 **방향**이
    뒤집혀 "붉어졌다"가 "푸르러졌다"로 나온다. 붉은 프레임으로 부호를 못 박는다."""
    red = features(_frame(b=10, g=10, r=200))
    assert red["log_rg"] > 1.0, "붉은 프레임인데 R/G 가 안 크다 — 채널이 뒤집혔다"
    assert red["log_bg"] < 0.5
    blue = features(_frame(b=200, g=10, r=10))
    assert blue["log_bg"] > 1.0
    assert blue["log_rg"] < 0.5


def test_features_measure_what_the_charts_need():
    dark = features(_frame(2, 2, 2))
    assert dark["luma"] < 5 and dark["dark_pct"] == 100.0
    bright = features(_frame(255, 255, 255))
    assert bright["luma"] > 250 and bright["sat_pct"] == 100.0
    assert len(bright["grid"]) == 9
    assert "ts" in bright


def test_a_steady_scene_never_alarms():
    """기준선과 같으면 영원히 조용하다 — 임계가 절대 단위인 이유의 반쪽이다
    (σ 기반이면 조용한 장면일수록 예민해진다)."""
    judge = Judge()
    out = _run(judge, [_feats(120)] * 30)
    assert all(not o for o in out)


def test_a_global_jump_alarms_after_three_samples_not_one():
    """급변은 **3샘플 연속**이어야 경보다 — 한 샘플짜리 노이즈(셔터·순간 가림)로
    경보를 내면 아무도 안 믿는다."""
    judge = Judge()
    seq = [_feats(120)] * 10 + [_feats(190)] * 6
    out = _run(judge, seq)
    assert not out[10], "점프 첫 샘플에 바로 경보를 냈다"
    fired = [i for i, o in enumerate(out) if o]
    assert fired and fired[0] in (12, 13), f"3연속께 떠야 한다: {fired[:3]}"
    assert out[fired[0]][0]["type"] == "brightness"
    assert out[fired[0]][0]["delta"] > 0


def test_an_arm_crossing_the_frame_is_not_a_lighting_event():
    """⚠ 손목 카메라는 팔이 화면 30~40%를 덮는 게 **정상 동작**이다. 전체 평균만
    보면 조명 급변과 같아 보인다 — 9칸 중 7칸이 같은 방향일 때만 조명이다."""
    judge = Judge()
    base = _feats(120)
    # 4칸만 크게 밝아진다(물체) — 평균은 +31 로 임계(20)를 넘는다
    local = _feats(120 + 70 * 4 / 9, grid=[190.0] * 4 + [120.0] * 5)
    out = _run(judge, [base] * 10 + [local] * 10)
    assert all(not o for o in out), "국소 변화(팔·물체)에 조명 경보를 냈다"


def test_the_alarm_clears_after_five_calm_samples():
    """해제도 히스테리시스다 — 경보가 깜빡거리면 아무도 안 읽는다."""
    judge = Judge()
    seq = [_feats(120)] * 10 + [_feats(190)] * 5 + [_feats(120)] * 12
    out = _run(judge, seq)
    assert out[14], "경보가 아예 안 떴다"
    tail = out[-3:]
    assert all(not o for o in tail), "복귀 후에도 경보가 안 걷혔다"


def test_a_color_shift_alarms_without_a_brightness_change():
    """색온도만 틀어지는 사건(WB 이동·다른 조명 켜짐)은 밝기로는 안 보인다."""
    judge = Judge()
    seq = [_feats(120)] * 10 + [_feats(120, rg=0.25)] * 6
    out = _run(judge, seq)
    fired = [o for o in out if o]
    assert fired and fired[0][0]["type"] == "color"
    assert fired[0][0]["delta_rg"] > 0


def test_warmup_does_not_judge():
    """스트림 시작 직후는 기준선이 없다 — 첫 화면부터 판정하면 그게 오보다."""
    judge = Judge(JudgeConfig(warmup_s=10.0))
    out = _run(judge, [_feats(120)] * 2 + [_feats(200)] * 3)
    assert all(not o for o in out)


def test_a_sustained_change_alarms_once_then_becomes_normal():
    """조명이 **바뀐 채 유지**되면 slow 가 새 값으로 수렴해 경보가 걷힌다 —
    "바뀌었다"를 알리는 것이지 "옛날과 다르다"를 영원히 우기는 게 아니다."""
    judge = Judge()
    out = _run(judge, [_feats(120)] * 10 + [_feats(190)] * 120)
    assert any(o for o in out), "경보가 아예 안 떴다"
    assert not out[-1], "한참 뒤에도 경보가 남아 있다 — slow 가 수렴하지 않는다"


def test_the_bus_contract_mirrors_vision():
    """발행 계약은 vision 의 복제다 — 최신값 키 + 역함수 (문서 §5)."""
    from piper_bus import contract as C

    assert C.light_key("top") == f"{C.LIGHT_PREFIX}:top"
    assert C.light_name(C.light_key("rs_123_color")) == "rs_123_color"
    # 샘플 주기(2초)보다 넉넉해야 한 틱 밀렸다고 깜빡이지 않는다
    assert C.LIGHT_TTL_MS >= 4000


def test_light_alerts_join_the_device_alert_flow(monkeypatch):
    """조명 경보는 **기존 장치 경보 경로에 합류한다** — add/clear 전이, WS 방송,
    재연결 시 목록 재조회를 전부 그 경로가 이미 한다 (문서 §5 소비자 1호)."""
    from app.services import light_watch as lw
    from app.services.device_watch import DeviceWatch

    monkeypatch.setattr(lw.light_watch, "_alerts",
                        [lw._brightness_alert("cam1", "탑뷰", -40.0)])
    watch = DeviceWatch()
    monkeypatch.setattr(watch, "_robots", lambda: [])
    monkeypatch.setattr(watch, "_cameras", lambda: [])
    new, gone = watch.check()
    assert len(new) == 1 and new[0].reason == "lighting"
    assert "어두워" in new[0].text
    # 걷히면 clear 로 나간다
    monkeypatch.setattr(lw.light_watch, "_alerts", [])
    new, gone = watch.check()
    assert not new and len(gone) == 1


def test_the_rest_mirror_serves_what_the_bus_gets(monkeypatch):
    """수집·추론 화면은 REST 미러를 폴링한다 — 버스에 발행되는 것과 같은 값."""
    from app.main import app
    from app.services import light_watch as lw

    monkeypatch.setattr(lw.light_watch, "_latest", {
        "cam1": {"id": "cam1", "label": "탑뷰", "ts": 1.0, "luma": 123.4,
                 "sat_pct": 0.0, "dark_pct": 0.0, "log_rg": 0.01,
                 "log_bg": -0.02, "grid": [123.4] * 9},
    })
    r = TestClient(app).get("/api/cameras/light")
    assert r.status_code == 200
    cams = r.json()["cameras"]
    assert cams[0]["label"] == "탑뷰" and cams[0]["luma"] == 123.4
