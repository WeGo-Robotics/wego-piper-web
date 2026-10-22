#!/usr/bin/env python3
"""__NAME__ — 미션 틀. 훅 넷만 채운다.

    piper-plugin run . --camera top=<스트림> --api http://127.0.0.1:8000 --dry-run
"""
from piper_plugin import Decision, MissionPlugin


class Main(MissionPlugin):
    def observe(self) -> dict:
        det = self.detections(self.config["detections_name"], fresh_s=2.0)
        return {"det": det}

    def decide(self, ctx) -> Decision:
        det = ctx["det"]
        if not det:
            return Decision.skip("검출 없음 (검출기가 돌고 있나?)")
        slots = self.judge(det.text, system=self.config["rules"])     # LLM → 슬롯 dict
        if not slots or not slots.get("slots"):
            return Decision.skip("판단 실패")
        s = slots["slots"]
        return Decision.task(f"pick up the {s.get('target')} and put it in the {s.get('destination')} bin", **s)

    def done(self, ctx) -> bool:
        if getattr(self, "_skip", False):     # 화면의 [skip] 버튼 → on_command
            self._skip = False
            return True
        return False                          # 완료 판정이 없으면 타임아웃으로 끝난다

    def record(self, ctx, result) -> dict:
        return {"detections": ctx["det"].text if ctx["det"] else None}

    def on_command(self, name, args):
        if name == "skip":
            self._skip = True                 # 기다리던 회차를 끝낸다


if __name__ == "__main__":
    raise SystemExit(Main.main())
