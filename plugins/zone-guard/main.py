#!/usr/bin/env python3
"""zone-guard — ROI 안에 사람이 들어오면 추론을 일시정지(또는 E-stop)하고, 비면 재개한다.

1층 예제. "읽기·검출·REST·안전" 네 축을 지난다. E-stop 은 열린 경로라 권한이 필요 없다.

    piper-plugin run plugins/zone-guard --camera watch=<스트림> --api http://127.0.0.1:8000 \
        --config '{"roi": "0.2,0.2,0.8,0.9"}'
"""

import time

from piper_plugin import Plugin


def _parse_roi(text: str) -> tuple[float, float, float, float]:
    parts = [float(v) for v in str(text).split(",")]
    if len(parts) != 4:
        raise ValueError("roi 는 x1,y1,x2,y2 네 값 (0~1)")
    x1, y1, x2, y2 = parts
    return min(x1, x2), min(y1, y2), max(x1, x2), max(y1, y2)


class ZoneGuard(Plugin):
    def setup(self):
        self.roi = _parse_roi(self.config["roi"])
        self.model = self.detector(self.config["model"], device=self.config["device"])
        self.cam = self.camera("watch")
        self.hits = 0
        self.paused = False
        self.clear_since: float | None = None
        self.interval = 1.0 / max(float(self.config["fps"]), 0.1)

    def _in_roi(self, box, w, h) -> bool:
        x1, y1, x2, y2 = self.roi
        cx, cy = (box[0] + box[2]) / 2 / w, (box[1] + box[3]) / 2 / h
        return x1 <= cx <= x2 and y1 <= cy <= y2

    def loop(self):
        import cv2

        t0 = time.monotonic()
        got = self.cam.read_new(timeout_s=1.0)
        if got is None:
            self.state(phase="waiting_camera")
            return
        frame, seq, wall_ns = got
        h, w = frame.shape[:2]
        r = self.model.predict(frame, conf=float(self.config["conf"]), device=self.config["device"], verbose=False)[0]
        boxes = r.boxes.xyxy.tolist()
        clss = r.boxes.cls.int().tolist()
        intruders = sum(1 for b, c in zip(boxes, clss)
                        if r.names[int(c)] == self.config["target_class"] and self._in_roi(b, w, h))

        canvas = r.plot()
        x1, y1, x2, y2 = self.roi
        cv2.rectangle(canvas, (int(x1 * w), int(y1 * h)), (int(x2 * w), int(y2 * h)),
                      (0, 0, 255) if intruders else (0, 200, 0), 2)
        self.publish_preview(f"det_{self.name}", canvas)

        self.hits = self.hits + 1 if intruders else 0
        if intruders:
            self.clear_since = None
        elif self.clear_since is None:
            self.clear_since = time.monotonic()

        if not self.paused and self.hits >= int(self.config["min_frames"]):
            if self.config["action"] == "estop":
                self.robot.estop()
                self.log.warning("ROI 침입 %d 프레임 — E-stop", self.hits)
                self.paused = True
            else:
                resp = self.robot.pause()
                self.paused = resp.ok
                self.log.warning("ROI 침입 %d 프레임 — 일시정지 (%s)", self.hits, resp.status)
        elif self.paused and self.config["action"] == "pause" and self.clear_since is not None \
                and time.monotonic() - self.clear_since >= float(self.config["resume_after_s"]):
            resp = self.robot.resume()
            self.paused = not resp.ok
            self.log.info("ROI 비었다 — 재개 (%s)", resp.status)

        self.state(phase="paused" if self.paused else "guarding", intruders=intruders)
        rest = self.interval - (time.monotonic() - t0)
        if rest > 0:
            self.sleep(rest)


if __name__ == "__main__":
    raise SystemExit(ZoneGuard.main())
