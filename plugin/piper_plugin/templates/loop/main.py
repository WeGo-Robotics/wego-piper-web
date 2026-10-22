#!/usr/bin/env python3
"""__NAME__ — 카메라 한 대를 읽어 프리뷰와 상태를 올리는 틀.

    piper-plugin run . --camera cam=<스트림> --api http://127.0.0.1:8000
"""
from piper_plugin import Plugin


class Main(Plugin):
    def setup(self):
        self.cam = self.camera("cam")          # 매니페스트 cameras.key
        self.frames = 0

    def loop(self):
        got = self.cam.read_new(timeout_s=1.0)
        if got is None:
            self.state(phase="waiting_camera")
            return
        frame, seq, ts = got                    # BGR
        self.frames += 1
        # ── 여기서 할 일을 한다 ──
        self.publish_preview(f"{self.name}_cam", frame)
        self.state(phase="running", frames=self.frames)
        self.sleep(float(self.config["interval_s"]))

    def teardown(self):
        pass


if __name__ == "__main__":
    raise SystemExit(Main.main())
