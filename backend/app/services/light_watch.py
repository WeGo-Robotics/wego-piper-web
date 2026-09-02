"""조명 감시 — 측정을 버스로 발행하고, 급변이면 장치 경보에 합류한다.

feature/lighting-watch.md §5·§6 의 발행자 + 알람 소비자 1호.

- **측정**: 연결된 카메라의 shm 세그먼트에서 최신 프레임을 읽어
  `piper_cam.lighting.features` 로 잰다 — 세그먼트가 곧 녹화·정책이 보는
  프레임이라 "정책이 보는 그대로"를 감시한다
- **발행**: `piper:light:<cam>` (vision 과 같은 최신값+TTL 규칙). 다른 소비자
  (수집·추론 화면, 뷰어, 나중의 오케스트레이터)는 이 계약만 본다
- **판정**: `piper_cam.lighting.Judge` (EWMA 두 개 + 전역성 + 히스테리시스).
  활성 경보는 `device_watch._collect()` 가 가져가 기존 add/clear·WS 경로를 탄다

호출은 main.py 의 장치 감시 루프가 2초마다 한다 (`sample()` — to_thread).
문구는 여기서만 만든다 — device_watch 와 같은 규칙이다.
"""

import logging
import time

from app.services.device_watch import Alert

logger = logging.getLogger(__name__)


def _brightness_alert(cam_id: str, name: str, delta: float) -> Alert:
    updown = "밝아" if delta > 0 else "어두워"
    return Alert(
        "camera", f"light:{cam_id}:brightness", name, "lighting",
        f"카메라 {name} 화면이 갑자기 {updown}졌습니다 (밝기 {delta:+.0f}/255). "
        "조명을 확인하세요 — 녹화 중이면 이 구간 에피소드가, 추론 중이면 "
        "동작 품질이 흔들릴 수 있습니다.")


def _color_alert(cam_id: str, name: str, d_rg: float, d_bg: float) -> Alert:
    # R/G 가 오르거나 B/G 가 내리면 따뜻한 쪽이다 — 더 크게 움직인 축으로 말한다
    warm = d_rg >= 0 if abs(d_rg) >= abs(d_bg) else d_bg < 0
    tone = "따뜻한(붉은)" if warm else "차가운(푸른)"
    return Alert(
        "camera", f"light:{cam_id}:color", name, "lighting",
        f"카메라 {name} 의 색이 갑자기 변했습니다 — {tone} 쪽 "
        f"(ΔR/G {d_rg:+.2f}, ΔB/G {d_bg:+.2f}, log₂). "
        "조명이 바뀌었거나 화이트밸런스가 움직인 것입니다. 데이터 색 일관성이 "
        "깨질 수 있으니 확인하세요.")


class LightWatch:
    """카메라별 조명 샘플러. 상태는 전부 여기 — Judge 는 카메라당 하나."""

    def __init__(self, bus=None) -> None:
        self._bus = bus
        self._explicit = bus is not None
        self._judges: dict[str, object] = {}
        self._latest: dict[str, dict] = {}
        self._alerts: list[Alert] = []

    def _connect(self):
        # preview_bridge 와 같은 지연 연결 — 버스가 없어도 측정·경보는 계속 된다
        if self._bus is None and not self._explicit:
            try:
                from piper_bus.client import Bus
                self._bus = Bus()
            except Exception as exc:
                logger.debug("light 버스 연결 실패 (발행만 빠진다): %s", exc)
        return self._bus

    def sample(self) -> None:
        """한 주기: 프레임 읽기 → 측정 → 발행 → 판정. **실패해도 던지지 않는다.**"""
        from piper_cam.lighting import Judge, features
        from piper_shm import Subscriber, segment_for_camera

        from app.services.camera_manager import camera_manager

        cams = {cid: c for cid, c in camera_manager.cameras.items() if c.connected}
        alerts: list[Alert] = []
        seen: set[str] = set()
        now = time.monotonic()
        for cam in cams.values():
            seg = segment_for_camera(cam.id)
            if seg.endswith("_depth"):
                continue           # 깊이에 밝기 감시는 무의미하다 (문서 §4)
            try:
                sub = Subscriber(seg)
            except Exception:
                continue           # 아직 발행 전 — 발행 멈춤 판정은 device_watch 몫
            try:
                got = sub.read()
            except Exception:
                continue
            finally:
                sub.close()
            if got is None:
                continue
            try:
                feats = features(got[0])
            except Exception as exc:
                logger.debug("조명 측정 실패 (%s): %s", cam.id, exc)
                continue
            seen.add(cam.id)
            label = cam.label or cam.name
            self._latest[cam.id] = {"id": cam.id, "label": label, **feats}

            bus = self._connect()
            if bus is not None:
                try:
                    bus.put_light(cam.id, feats)
                except Exception as exc:
                    logger.debug("light 발행 실패 (%s): %s", cam.id, exc)

            judge = self._judges.get(cam.id)
            if judge is None:
                judge = self._judges[cam.id] = Judge()
            for p in judge.update(feats, now):
                if p["type"] == "brightness":
                    alerts.append(_brightness_alert(cam.id, label, p["delta"]))
                else:
                    alerts.append(_color_alert(cam.id, label, p["delta_rg"], p["delta_bg"]))

        # 끊긴 카메라는 상태를 버린다 — 재연결하면 기준선을 새로 잡는다(워밍업).
        # 몇 시간 전 기준선으로 재연결 직후를 판정하면 그게 오보다.
        for cid in list(self._judges):
            if cid not in seen:
                del self._judges[cid]
                self._latest.pop(cid, None)
        self._alerts = alerts

    def alerts(self) -> list[Alert]:
        """활성 조명 경보. device_watch 가 자기 목록에 합쳐 전이를 계산한다."""
        return list(self._alerts)

    def latest(self) -> list[dict]:
        """REST 미러용 — 마지막 샘플의 카메라별 측정값."""
        return sorted(self._latest.values(), key=lambda d: d["label"])


light_watch = LightWatch()
