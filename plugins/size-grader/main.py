#!/usr/bin/env python3
"""size-grader — 검출 입력 → 크기 등급 → 토픽 `grades` (feature/plugins.md §7-3).

사과 분류기의 "등급" 칸을 가장 작게 흉내 낸 것이다. 모델 없이 bbox 의 짧은 변으로 대·중·소를
나눈다 — 플러그인 하나가 다른 플러그인의 출력을 받아 또 다른 출력으로 넘기는 **배선**이 요점이다.

    piper-plugin run plugins/size-grader --api http://127.0.0.1:8000     (검출기가 돌고 있어야 한다)
"""

from __future__ import annotations

from piper_plugin import Plugin


class SizeGrader(Plugin):
    def setup(self):
        self.source = self.input("det")            # 시작할 때 고른 검출 이름 (기본 "top")
        self.last_seq = None
        self.state(source=self.source, counts={})

    def grade(self, box) -> str:
        x1, y1, x2, y2 = box
        short = min(x2 - x1, y2 - y1)
        if short >= float(self.config["large_px"]):
            return "large"
        if short <= float(self.config["small_px"]):
            return "small"
        return "medium"

    def loop(self):
        det = self.input_detections("det", fresh_s=float(self.config["fresh_s"]))
        if det is None:
            self.state(phase="waiting_input")
            self.sleep(0.5)
            return
        seq = det.raw.get("frame_seq")
        if seq == self.last_seq:                   # 같은 프레임 — 다시 매기지 않는다
            self.sleep(0.05)
            return
        self.last_seq = seq
        graded = [{"label": o["label"], "conf": o["conf"], "bbox": o["bbox"], "grade": self.grade(o["bbox"])}
                  for o in det.objects]
        counts: dict[str, int] = {}
        for g in graded:
            counts[g["grade"]] = counts.get(g["grade"], 0) + 1
        text = ", ".join(f"{g['label']}:{g['grade']}" for g in graded) or "없음"
        self.publish("grades", {"source": self.source, "frame_seq": seq, "objects": graded,
                                "counts": counts, "text": text})
        self.state(phase="grading", counts=counts)


if __name__ == "__main__":
    raise SystemExit(SizeGrader.main())
