#!/usr/bin/env python3
"""detect-and-start — 지정 클래스가 N 프레임 연속 보이면 추론을 켜고, 사라지면 끈다.

1층(`Plugin.loop`) 예제. 검출은 플러그인 안에서(RT-DETR), 팔은 REST 로만 움직인다.

    piper-plugin run plugins/detect-and-start --camera watch=<스트림> --api http://127.0.0.1:8000 \
        --config '{"checkpoint": "/path/to/checkpoint", "target_class": "bottle"}'
"""

import time

from piper_plugin import Plugin


class DetectAndStart(Plugin):
    def setup(self):
        if not self.config.get("checkpoint"):
            self.stop("checkpoint 가 비어 있다 — 설정에서 체크포인트를 고르라")
        self.model = self.detector(self.config["model"], device=self.config["device"])
        self.cam = self.camera("watch")
        self.seen = 0
        self.running = False
        self.interval = 1.0 / max(float(self.config["fps"]), 0.1)

    def loop(self):
        t0 = time.monotonic()
        got = self.cam.read_new(timeout_s=1.0)
        if got is None:
            self.state(phase="waiting_camera")
            return
        frame, seq, wall_ns = got
        r = self.model.predict(frame, conf=float(self.config["conf"]), device=self.config["device"], verbose=False)[0]
        labels = [r.names[int(c)] for c in r.boxes.cls]
        self.publish_detections(self.name, frame.shape[:2], r, seq=seq, wall_ns=wall_ns, model=self.config["model"])
        self.publish_preview(f"det_{self.name}", r.plot())

        self.seen = self.seen + 1 if self.config["target_class"] in labels else 0
        self.state(phase="running" if self.running else "watching", seen=self.seen)

        if not self.running and self.seen >= int(self.config["min_frames"]):
            resp = self.robot.start_inference(
                self.config["checkpoint"], self.camera_mapping(),
                policy_type=self.config.get("policy_type") or "act",
                task=self.config.get("task") or None,
            )
            if resp.ok:
                self.running = True
                self.log.info("추론 시작 — %s 가 %d 프레임 연속", self.config["target_class"], self.seen)
            elif resp.status == 409:
                self.log.warning("추론 시작 거부(배타): %s", resp.detail)   # 학습 중 등 — 사람이 볼 로그
                self.sleep(5)
            else:
                self.log.error("추론 시작 실패 %s: %s", resp.status, resp.detail or resp.text[:200])
                self.sleep(5)
        elif self.running:
            alive = self.robot.inference_running()
            if alive is False:
                self.running = False                                    # E-stop 등으로 밖에서 멈췄다
                self.log.info("추론이 멈췄다 — 다시 감시")
            elif self.config["auto_stop"] and self.seen == 0:
                self.robot.stop_inference()
                self.running = False
                self.log.info("물체가 사라져 추론 정지")

        rest = self.interval - (time.monotonic() - t0)
        if rest > 0:
            self.sleep(rest)

    def teardown(self):
        if self.running:
            self.robot.stop_inference()


if __name__ == "__main__":
    raise SystemExit(DetectAndStart.main())
