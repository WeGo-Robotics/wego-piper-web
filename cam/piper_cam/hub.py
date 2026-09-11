"""v4l2 카메라 소유 — camerad 가 돌리는 허브.

## RealSenseHub 와 **같은 메서드 이름**을 쓴다

게이트웨이의 `CameraInfo` 가 `cam_type` 으로 허브만 고르면 되게 하려는 것이다.
이름이 갈리면 호출부마다 분기가 생기고, 그 분기가 곧 두 번째 진실이 된다.

    scan · connect · disconnect · release_all · probe · list_controls · set_control
    · measure_gray_card · calibrate_gray_card

프레임은 여기 없다 — `/dev/shm` 세그먼트로 나가고 소비자가 직접 읽는다.
"""

import logging
import os
import threading
import time

from piper_cam import controls as controls_mod
from piper_cam import v4l2

logger = logging.getLogger(__name__)


class _V4l2Camera:
    """카메라 하나. 열면 백그라운드로 계속 읽어 세그먼트에 흘린다."""

    def __init__(self, dev_path: str) -> None:
        self.id = dev_path
        self._cap = None
        self._thread: threading.Thread | None = None
        self._running = False
        self._lock = threading.Lock()
        self.width: int | None = None
        self.height: int | None = None
        self.fps: int | None = None
        # 요청 프로파일 `(w, h, fps)`. None 이면 드라이버 기본값.
        self._want: tuple[int, int, int] | None = None
        # 장치가 사라졌다고 **데몬이 판정한** 시각. 게이트웨이가 추론하지 않게 하려고
        # 여기서 결론을 낸다 (`lost()` RPC 로 나간다).
        self.lost_at: float = 0.0
        # 마지막으로 읽은 프레임(BGR) — 회색 카드 측정이 쓴다. 발행은 shm 으로 나가지만
        # 세그먼트를 되읽는 것보다 여기 한 장 들고 있는 편이 싸고 정확하다.
        self._last = None

    @property
    def connected(self) -> bool:
        return self._cap is not None

    def get_frame(self):
        """마지막으로 읽은 프레임(BGR) — 없으면 None. rsd 의 `dev.get_frame` 에 해당한다."""
        return self._last

    def _open(self):
        import cv2

        cap = cv2.VideoCapture(self.id, cv2.CAP_V4L2)
        if not cap.isOpened():
            return None, None
        # 요청 프로파일 적용. **드라이버가 거절해도 조용히 무시한다** — 아래에서
        # 실제 값을 다시 읽으므로, 못 맞춘 채로 맞췄다고 착각할 일은 없다.
        if self._want:
            w, h, fps = self._want
            cap.set(cv2.CAP_PROP_FRAME_WIDTH, w)
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, h)
            cap.set(cv2.CAP_PROP_FPS, fps)
        ok, frame = cap.read()
        if not ok or frame is None:
            cap.release()
            return None, None
        self.width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        self.height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        self.fps = int(cap.get(cv2.CAP_PROP_FPS)) or 30
        return cap, frame

    def connect(self, want: tuple[int, int, int] | None = None) -> tuple[bool, str]:
        # 이미 열려 있는데 다른 프로파일을 요청하면 다시 연다 — 안 그러면
        # UI 에서 해상도를 바꿔도 예전 설정 그대로 돈다.
        if self.connected and want is not None and want != self._want:
            self.disconnect()
        if want is not None:
            self._want = want
        if self.connected:
            return True, "OK"
        try:
            cap, frame = self._open()
        except Exception as exc:
            return False, str(exc)
        if cap is None:
            return False, f"Cannot open {self.id}"
        self._cap = cap
        self._running = True
        self._last = frame
        self._publish(frame)
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        return True, "OK"

    def _publish(self, frame) -> None:
        from piper_cam.publish import publish_frame

        publish_frame(self.id, frame)

    # 장치 노드가 사라졌는지 보는 주기. `os.path.exists` 라 사실상 공짜다.
    _PRESENCE_S = 1.0
    # 노드는 남아 있는데 읽기만 계속 실패하는 경우의 상한 — **시간 기준**이다.
    #
    # ⚠ 횟수로 세면 안 된다. 실패한 `cap.read()` 는 **즉시** 돌아오므로 30회가
    # 몇 밀리초 만에 찬다 — 일시적인 딸꾹질에도 "사라졌다"를 선언하게 된다.
    # 게다가 그 사이 루프가 전력으로 돌아 CPU 를 태운다 (실기에서 camerad 가
    # 하루도 안 돼 CPU 4시간 41분을 썼다).
    _FAIL_GRACE_S = 3.0
    # 실패했을 때 쉬는 시간. 안 쉬면 위와 같은 폭주가 된다.
    _FAIL_SLEEP_S = 0.05

    def _loop(self) -> None:
        """읽고 발행한다. **장치가 없어지면 스스로 결론을 낸다.**

        예전에는 실패하면 그냥 계속 돌았다. 그래서 USB 를 뽑아도 발행만 멈추고
        세그먼트는 남아, 게이트웨이가 "오래됐다"로 **추론**해야 했다 —
        늦고, 읽기가 계속 성공하는 장치에서는 아예 못 잡았다.
        소유자가 판정하는 편이 빠르고 확실하다.
        """
        last_check = time.monotonic()
        failing_since = 0.0
        while self._running and self._cap:
            try:
                ok, frame = self._cap.read()
            except Exception:
                ok, frame = False, None
            if ok and frame is not None:
                self._last = frame
                self._publish(frame)
                failing_since = 0.0
            else:
                if not failing_since:
                    failing_since = time.monotonic()
                time.sleep(self._FAIL_SLEEP_S)      # 폭주 방지

            now = time.monotonic()
            if now - last_check >= self._PRESENCE_S:
                last_check = now
                # ⚠ 노드가 사라진 것이 **결정적 증거**다 — USB 를 뽑으면
                # `/dev/videoN` 이 즉시 없어진다. 읽기 실패는 일시적일 수 있다.
                if not os.path.exists(self.id):
                    self._declare_lost("장치 노드가 사라졌습니다")
                    return
            if failing_since and now - failing_since >= self._FAIL_GRACE_S:
                self._declare_lost(f"{self._FAIL_GRACE_S:.0f}초간 프레임을 읽지 못했습니다")
                return

    def _declare_lost(self, why: str) -> None:
        """장치가 없어졌다고 판정하고 **발행을 끊는다.**

        세그먼트를 지우는 것이 중요하다 — 남겨두면 소비자가 열어놓고 멈춘 화면을
        본다. 등록·설정은 게이트웨이가 들고 있으므로 여기서 지울 것은 없다.
        """
        logger.warning("%s: %s — 발행을 중단합니다", self.id, why)
        self.lost_at = time.time()
        self._running = False
        if self._cap is not None:
            try:
                self._cap.release()
            except Exception:
                pass
            self._cap = None
        from piper_cam.publish import stop as stop_publish

        stop_publish(self.id)

    def disconnect(self) -> None:
        from piper_cam.publish import stop as stop_publish

        # 사용자가 일부러 닫는 것 — 사라진 게 아니므로 판정을 지운다
        self.lost_at = 0.0

        # ⚠ **순서가 중요하다.** 읽기 스레드를 먼저 멈춘다 — 발행자를 먼저 닫으면
        # 그 사이 루프가 한 프레임 더 발행해 **세그먼트를 되살린다.**
        # (rsd 에서 실제로 겪은 버그다.)
        self._running = False
        t = self._thread
        if t and t.is_alive():
            t.join(timeout=2)
        self._thread = None
        if self._cap is not None:
            try:
                self._cap.release()
            except Exception:
                pass
            self._cap = None
        stop_publish(self.id)

    def probe(self, timeout: float = 3.0) -> tuple[bool, str]:
        """연결 테스트 + 프레임 1장 → 즉시 해제. 스캔용."""
        import concurrent.futures

        def _do():
            cap, frame = self._open()
            if cap is None:
                return False, f"Cannot open {self.id}"
            self._publish(frame)      # 스캔 썸네일 — 세그먼트를 지우지 않고 남긴다
            cap.release()
            return True, "OK"

        try:
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
                return ex.submit(_do).result(timeout=timeout)
        except concurrent.futures.TimeoutError:
            return False, f"Timeout ({timeout}s) probing {self.id}"
        except Exception as exc:
            return False, str(exc)


class V4l2Hub:
    def __init__(self) -> None:
        self.cams: dict[str, _V4l2Camera] = {}
        self._info: dict[str, dict] = {}
        # 마지막 프로파일 적용 결과 — 연결 안에서 적용하므로 응답에 실을 수 없다.
        # 화면이 "몇 개 적용/잠김/실패"를 보려면 여기서 가져가야 한다.
        self._last_apply: dict[str, dict] = {}

    def scan(self) -> list[dict]:
        found = v4l2.scan_cameras()
        for d in found:
            self._info[d["id"]] = d
            self.cams.setdefault(d["id"], _V4l2Camera(d["id"]))
        return found

    def _cam(self, cam_id: str) -> _V4l2Camera | None:
        if cam_id not in self.cams and cam_id.startswith("/dev/"):
            self.cams[cam_id] = _V4l2Camera(cam_id)
        return self.cams.get(cam_id)

    def connect(self, cam_id: str, width: int = 0, height: int = 0,
                fps: int = 0, controls: dict | None = None) -> tuple[bool, str]:
        """셋 다 주면 그 프로파일로 연다. 하나라도 0이면 드라이버 기본값.

        `controls` 를 주면 **스트림을 연 뒤** 밀어 넣는다. 순서가 그래야 하는 이유:
        일부 UVC 웹캠은 `STREAMON` 에서 자동 노출을 다시 켠다. 열기 전에 넣으면
        그 순간 되돌아가고, 사용자는 "저장했는데 왜 또 초기화되나"를 다시 겪는다.

        적용이 실패해도 연결은 성공으로 돌려준다 — 조명이 틀린 영상이
        안 나오는 영상보다 낫다. 결과는 `last_apply_report()` 로 확인한다.
        """
        cam = self._cam(cam_id)
        if not cam:
            return False, f"Unknown camera: {cam_id}"
        want = (int(width), int(height), int(fps)) if width and height and fps else None
        ok, msg = cam.connect(want)
        if ok and controls:
            self.apply_controls(cam_id, controls)
        return ok, msg

    def disconnect(self, cam_id: str) -> None:
        cam = self.cams.get(cam_id)
        if cam:
            cam.disconnect()

    def release_all(self) -> bool:
        released = False
        for cam in self.cams.values():
            if cam.connected:
                cam.disconnect()
                released = True
        return released

    def probe(self, cam_id: str) -> tuple[bool, str]:
        cam = self._cam(cam_id)
        return cam.probe() if cam else (False, f"Unknown camera: {cam_id}")

    def list_controls(self, cam_id: str) -> list[dict]:
        return v4l2.v4l2_list_controls(cam_id)

    def set_control(self, cam_id: str, name: str, value: float) -> bool:
        for ctrl in v4l2.v4l2_list_controls(cam_id):
            if ctrl["name"] == name:
                return v4l2.v4l2_set_control(cam_id, ctrl["cid"], int(value))
        return False

    def apply_controls(self, cam_id: str, wanted: dict,
                       budget_s: float = 2.0) -> dict:
        """프로파일 컨트롤을 순서대로 적용하고 read-back 으로 검증한다.

        순서 규칙은 rsd 와 **한 벌을 공유한다**(`piper_cam.controls`) — 자동 모드
        종속성은 v4l2 든 RealSense 든 같은 함정이라, 두 벌이면 한쪽만 고쳐진다.
        """
        cids = {c["name"]: c["cid"] for c in v4l2.v4l2_list_controls(cam_id)}

        def _set(name: str, value) -> bool:
            # v4l2 컨트롤은 정수다. 실수를 받는 건 RealSense 쪽뿐이라
            # 계약(`piper_cam.controls`)은 실수를 통과시키고 여기서 좁힌다.
            cid = cids.get(name)
            return v4l2.v4l2_set_control(cam_id, cid, int(value)) if cid is not None else False

        report = controls_mod.apply_controls(
            lambda: v4l2.v4l2_list_controls(cam_id), _set, wanted,
            budget_s=budget_s, label=cam_id,
        )
        self._last_apply[cam_id] = report
        return report

    def last_apply_report(self, cam_id: str) -> dict:
        return self._last_apply.get(cam_id, {})

    # ── 회색 카드 (feature/gray-card-calibration.md) ──────────────────────────
    # rsd 와 **같은 절차**다 — 계산은 piper_cam.graycard 한 벌이고, 다른 것은 컨트롤 이름과
    # 자동 스위치의 값뿐이다. ⚠ USB 웹캠으로 눌러 보니 "Not a RealSense id" 였다(2026-09-11):
    # 동사가 rsd 에만 있었고 게이트웨이도 rsd 로만 보냈다.
    _CAL_SETTLE_S = 2.0     # 자동 노출이 자리 잡는 시간
    _CAL_ROUNDS = 3         # read-back 반복 상한
    # V4L2 이름은 커널 세대·장치마다 다르다 — 있는 것을 고른다. 없으면 그 단계는 건너뛰고
    # 보고(`skipped`)에 적는다 — 값싼 웹캠은 수동 WB 나 gain 이 아예 없다.
    _AE_NAMES = ("auto_exposure", "exposure_auto")
    _AWB_NAMES = ("white_balance_automatic", "white_balance_temperature_auto")
    _WB_NAMES = ("white_balance_temperature",)
    _EXPOSURE_NAMES = ("exposure_time_absolute", "exposure_absolute")
    _GAIN_NAMES = ("gain",)

    @staticmethod
    def _pick(controls: list[dict], names, writable: bool = True) -> dict | None:
        by = {c.get("name"): c for c in controls}
        for n in names:
            c = by.get(n)
            if c is not None and not (writable and c.get("readonly")):
                return c
        return None

    @staticmethod
    def _auto_value(ctrl: dict) -> int:
        """이 스위치를 **자동**으로 돌리는 값. bool 은 1, menu(auto_exposure)는 3 —
        V4L2_EXPOSURE_APERTURE_PRIORITY. UVC 웹캠이 지원하는 자동은 보통 이것뿐이다
        (0 = V4L2_EXPOSURE_AUTO 는 대개 목록에 없다). 수동은 controls.manual_value 가 안다."""
        if ctrl.get("type") == 2:
            return 1
        lo, hi = ctrl.get("min", 0), ctrl.get("max", 3)
        return 3 if lo <= 3 <= hi else int(hi)

    @staticmethod
    def _num(v, d: float) -> float:
        # ⚠ `or` 로 거르면 안 된다 — gain 은 0 이 유효값이다. None 만 결측이다.
        return d if v is None else float(v)

    def measure_gray_card(self, cam_id: str, roi=None) -> dict:
        """카드 영역을 **재기만** 한다. 장치는 안 건드린다 — rsd 와 같은 보고."""
        from piper_cam import graycard as gc

        cam = self.cams.get(cam_id)
        if cam is None or not cam.connected:
            return {"ok": False, "error": f"{cam_id} 가 연결돼 있지 않습니다"}
        frame = cam.get_frame()
        if frame is None:
            return {"ok": False, "error": "프레임을 받지 못했습니다"}
        try:
            reading = gc.measure(frame, tuple(roi) if roi else None)
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}
        ok, verdict = reading.verdict()
        return {"ok": ok, "verdict": verdict, "reading": reading.to_dict(),
                "frame": [int(frame.shape[1]), int(frame.shape[0])],
                "roi": list(roi) if roi else list(gc.center_roi(frame.shape))}

    def calibrate_gray_card(self, cam_id: str, roi=None, target: float | None = None,
                            adjust: str = "exposure") -> dict:
        """회색 카드로 WB·밝기를 맞춘다 — `RealSenseHub.calibrate_gray_card` 와 같은 절차.

        1. 자동 노출만 켜고 기다린다 (AWB 는 끈다 — 수렴값이 읽히지 않는다, rsd 주석)
        2. 자동을 끈다 — AE 가 찾은 노출이 얼어붙는다
        3. WB 를 카드로 직접 맞춘다 (`white_balance_temperature`)
        4. 밝기 손잡이 하나 — `exposure_time_absolute`(×100µs) 또는 `gain` — 를 비례 보정한다
        5. 다시 재서 보고한다

        ⚠ V4L2 `auto_exposure` 는 값이 반직관적이다(1 = 수동, 3 = 자동) — controls.py.
        ⚠ 값싼 웹캠은 수동 WB·gain 이 없다 — 그 단계는 건너뛰고 `skipped` 에 적는다.
        값을 저장하지는 않는다 — 저장은 프로파일이 한다.
        """
        from piper_cam import graycard as gc

        cam = self.cams.get(cam_id)
        if cam is None or not cam.connected:
            return {"ok": False, "error": f"{cam_id} 가 연결돼 있지 않습니다"}
        if adjust not in ("exposure", "gain"):
            return {"ok": False, "error": f"모르는 보정 손잡이: {adjust!r}"}
        target = gc.TARGET_LUMA if target is None else float(target)
        steps: list[dict] = []
        skipped: list[str] = []

        def _controls() -> list[dict]:
            return v4l2.v4l2_list_controls(cam_id)

        def _read():
            frame = cam.get_frame()
            return None if frame is None else gc.measure(frame, tuple(roi) if roi else None)

        def _range(name: str, fallback):
            for c in _controls():
                if c.get("name") == name:
                    return (self._num(c.get("min"), fallback[0]), self._num(c.get("max"), fallback[1]),
                            self._num(c.get("value"), fallback[2]))
            return fallback

        ctrls = _controls()
        ae = self._pick(ctrls, self._AE_NAMES)
        awb = self._pick(ctrls, self._AWB_NAMES)
        wb = self._pick(ctrls, self._WB_NAMES)
        knob = self._pick(ctrls, self._EXPOSURE_NAMES if adjust == "exposure" else self._GAIN_NAMES)
        ae_manual = controls_mod.manual_value(ae) if ae is not None else None
        if ae is not None and ae_manual is None:
            ae_manual = 1

        # 1) 자동 노출만 수렴시킨다. gain 모드는 노출 고정 약속이라 AE 를 끈다. AWB 는 끈다.
        if ae is not None:
            self.set_control(cam_id, ae["name"], ae_manual if adjust == "gain" else self._auto_value(ae))
        else:
            skipped.append("자동 노출 스위치 없음")
        if awb is not None:
            self.set_control(cam_id, awb["name"], 0)
        time.sleep(self._CAL_SETTLE_S if adjust == "exposure" else 0.5)

        before = _read()
        if before is None:
            return {"ok": False, "error": "프레임을 받지 못했습니다"}
        usable, why = before.usable
        if not usable:
            # 못 믿을 측정으로 값을 정하면 다음 주에 재현이 안 된다 — 이 기능의 존재 이유다
            return {"ok": False, "error": why, "before": before.to_dict()}

        # 2) 자동을 끈다 — 노출이 얼어붙는다
        if ae is not None:
            self.set_control(cam_id, ae["name"], ae_manual)
            time.sleep(0.4)

        # 3) WB 를 카드로 직접 맞춘다
        reading = _read() or before
        if wb is None:
            skipped.append("수동 white_balance_temperature 없음 — WB 는 못 맞춘다")
        else:
            wb_lo, wb_hi, wb_cur = _range(wb["name"], (2800.0, 6500.0, 4600.0))
            for _ in range(self._CAL_ROUNDS):
                if reading.neutral_error_pct <= gc.NEUTRAL_TOLERANCE_PCT:
                    break
                new_wb = gc.white_balance_for(reading, max(wb_cur, 1.0), wb_lo, wb_hi)
                if abs(new_wb - wb_cur) < 10:
                    break
                self.set_control(cam_id, wb["name"], new_wb)
                wb_cur = new_wb
                time.sleep(0.4)
                reading = _read() or reading
                steps.append({"white_balance": round(wb_cur),
                              "neutral_pct": round(reading.neutral_error_pct, 1),
                              "luma": round(reading.luma, 1)})

        # 4) 밝기 손잡이 하나. V4L2 노출은 ×100µs 단위(controls.py), gain 은 원시값.
        if knob is None:
            skipped.append(("exposure_time_absolute" if adjust == "exposure" else "gain") + " 없음 — 밝기는 못 맞춘다")
        else:
            lo, hi, cur = _range(knob["name"], (1.0, 5000.0, 100.0) if adjust == "exposure" else (0.0, 255.0, 16.0))
            cur = max(cur, 1.0)          # 비례 보정은 0 에 갇힌다
            scale = 100 if adjust == "exposure" else 1
            for _ in range(self._CAL_ROUNDS):
                if abs(reading.luma - target) <= gc.LUMA_TOLERANCE:
                    break
                new = gc.exposure_for(reading, cur, max(lo, 1.0), hi, target)
                if abs(new - cur) < 1:
                    break
                self.set_control(cam_id, knob["name"], new)
                cur = new
                time.sleep(0.4)
                reading = _read() or reading
                steps.append({("gain" if adjust == "gain" else "exposure_us"): round(cur * scale),
                              "luma": round(reading.luma, 1)})

        # 보고 — 두 손잡이의 최종값을 다 싣는다. 화면이 "무엇이 움직였나"를 말하려면 안 움직인
        # 쪽 값도 알아야 한다. rsd 와 같은 키. 없는 손잡이는 0.
        final = _controls()
        exp_c = self._pick(final, self._EXPOSURE_NAMES, writable=False)
        gain_c = self._pick(final, self._GAIN_NAMES, writable=False)
        wb_c = self._pick(final, self._WB_NAMES, writable=False)
        exp_us = self._num(exp_c.get("value"), 0.0) * 100 if exp_c else 0.0
        gain_now = self._num(gain_c.get("value"), 0.0) if gain_c else 0.0
        wb_now = self._num(wb_c.get("value"), 0.0) if wb_c else 0.0
        ok, verdict = reading.verdict(target)
        if skipped:
            verdict = f"{verdict} (건너뜀: {'; '.join(skipped)})"
        logger.info("회색 카드 보정 %s (%s): %s (노출 %.0fus, gain %.0f, WB %.0fK, 밝기 %.0f, 치우침 %.1f%%)",
                    cam_id, adjust, verdict, exp_us, gain_now, wb_now,
                    reading.luma, reading.neutral_error_pct)
        frame = cam.get_frame()
        return {"ok": ok, "verdict": verdict, "target": target, "adjust": adjust,
                "before": before.to_dict(), "after": reading.to_dict(),
                "exposure_us": round(exp_us), "gain": round(gain_now),
                "white_balance": round(wb_now), "steps": steps, "skipped": skipped,
                "roi": list(roi) if roi else list(gc.center_roi(
                    frame.shape if frame is not None else (480, 640, 3)))}

    def lost(self) -> list[dict]:
        """**데몬이 판정한** 사라진 장치들. 게이트웨이가 추론하지 않게 하려는 것이다.

        `disconnect()` 로 닫은 것은 여기 안 들어온다 — 사라진 것과 닫은 것은 다르다.
        """
        return [{"id": cam_id, "at": cam.lost_at}
                for cam_id, cam in self.cams.items() if cam.lost_at]

    def info(self, cam_id: str) -> dict:
        """해상도 등 — 연결 중이면 실제 값, 아니면 스캔 값."""
        cam = self.cams.get(cam_id)
        base = dict(self._info.get(cam_id) or {})
        base["connected"] = bool(cam and cam.connected)
        # 이미 반영한 요청 — rsd 와 같은 계약이다(게이트웨이가 둘을 구분 안 한다)
        base["want"] = list(cam._want or ()) if cam else []
        if cam and cam.connected:
            base.update(width=cam.width, height=cam.height, fps=cam.fps)
        return base
