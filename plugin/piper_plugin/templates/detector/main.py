#!/usr/bin/env python3
"""__NAME__ — 검출기 틀. RT-DETR(Apache) 기본. ultralytics(.pt) 는 쓰지 않는다 (AGPL).

    piper-plugin run . --camera cam=<스트림>
"""
import time

from piper_plugin import Plugin


class Main(Plugin):
    def setup(self):
        self.model = self.detector(self.config["model"], device=self.config["device"])
        self.cam = self.camera("cam")
        self.interval = 1.0 / max(float(self.config["fps"]), 0.1)
        self.last = time.monotonic()

    def loop(self):
        t0 = time.monotonic()
        got = self.cam.read_new(timeout_s=1.0)
        if got is None:
            self.state(phase="waiting_camera")
            return
        frame, seq, wall_ns = got
        r = self.model.predict(frame, conf=float(self.config["conf"]), imgsz=int(self.config["imgsz"]),
                               device=self.config["device"], verbose=False)[0]
        self.publish_detections(self.name, frame.shape[:2], r, seq=seq, wall_ns=wall_ns, model=self.config["model"])
        self.publish_preview(f"det_{self.name}", r.plot())
        now = time.monotonic()
        self.state(phase="detecting", det_fps=round(1.0 / max(now - self.last, 1e-3), 1), objects=len(r.boxes.cls))
        self.last = now
        rest = self.interval - (now - t0)
        if rest > 0:
            self.sleep(rest)


if __name__ == "__main__":
    raise SystemExit(Main.main())
