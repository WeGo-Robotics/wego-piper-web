#!/usr/bin/env python3
"""recycling — 분리수거 루프. `MissionPlugin` 위에 훅 넷 (feature/plugins.md §12-1).

오케스트레이터 1단계(`services/orchestrator.py`, 삭제됨)가 하던 것 그대로:

    capture(버스의 신선한 검출) → judge(LLM, 실패는 회차 스킵) → set_task(언어 정책) 또는
    skill(체크포인트 교체) → wait(타임아웃 — 완료 판정 센서가 아직 없다) → 기록 → reset

다른 점은 **자리**다. 게이트웨이 안의 async 상태기계가 아니라 별도 프로세스이고, 판단은
`POST /api/vision/judge`, task 는 `POST /api/params` 로 간다. E-stop·추론 사망·정지 요청
감시, 저널, 정책 종류 검사는 뼈대(`MissionPlugin`)가 한다.

    piper-plugin run plugins/recycling --api http://127.0.0.1:8000 --dry-run
"""

from __future__ import annotations

import time

from piper_plugin import Decision, MissionPlugin


class Recycling(MissionPlugin):
    def setup(self):
        cams = [c.strip() for c in str(self.config.get("cams") or "").split(",") if c.strip()]
        self.cams = cams                       # 빈 리스트 = 살아 있는 검출 전부
        self.rules = str(self.config.get("rules") or "") or None     # None → 게이트웨이 기본 규칙
        self.template = str(self.config.get("task_template") or "pick up the {target} and put it in the {destination}")
        self._skip = False
        # 검출기가 사는지 한 번 본다 — 루프 안에서 매 회차 "검출 없음" 으로 헛도는 것보다 낫다
        if not self.dry_run and not self._fresh():
            self.log.warning("살아 있는 검출이 없다 — 검출기를 켜라. 첫 회차는 검출을 기다린다")

    # ── 훅 ──

    def _fresh(self) -> dict[str, str]:
        """검출 이름 → 프롬프트 한 줄. 낡은 검출로 판단하는 사고를 막는 선이다."""
        fresh_s = float(self.config.get("freshness_s") or 3.0)
        out: dict[str, str] = {}
        try:
            names = self.bus.detection_names()
        except Exception as e:
            self.log.warning("버스 읽기 실패: %s", e)
            return out
        for name in names:
            if self.cams and name not in self.cams:
                continue
            d = self.detections(name, fresh_s=fresh_s)
            if d is not None:
                out[name] = d.text
        return out

    def observe(self) -> dict:
        texts = self._fresh()
        if not texts and self.dry_run:
            texts = {"(dry)": "검출 없음"}
        return {"detections": texts, "text": "\n".join(texts.values())}

    def decide(self, ctx) -> Decision:
        if not ctx["detections"]:
            return Decision.skip("no_fresh_detections")          # 검출기가 죽었거나 아직이다
        body = {"user": ctx["text"], "timeout_s": 30.0}
        if self.rules:
            body["system"] = self.rules
        if self.config.get("provider"):
            body["provider"] = self.config["provider"]
        if self.config.get("model"):
            body["model"] = self.config["model"]
        skills = self.config.get("skills") or {}
        if skills:
            body["skills"] = skills                             # LLM 이 고를 수 있는 값이 곧 스킬 이름
        r = self.robot.api.post("/api/vision/judge", json=body, timeout_s=35)
        if not r.ok:
            return Decision.skip(f"judge_failed: {r.detail or r.status}")   # LLM 은 단일 장애점이 아니다
        slots = (r.json() or {}).get("slots") or {}
        if not slots or slots.get("target") in (None, "", "none"):
            return Decision.skip("nothing_to_pick", )
        if skills:
            skill = slots.get("skill")
            if skill not in skills:
                return Decision.skip(f"unknown_skill: {skill}")
            return Decision.skill(skill, **slots)
        return Decision.task(self.template.format(target=slots["target"], destination=slots["destination"]), **slots)

    def done(self, ctx) -> bool:
        if self._skip:                                          # [skip] 버튼
            self._skip = False
            return True
        return False                                            # 완료 판정 센서(G3)가 오면 여기만 바뀐다

    def record(self, ctx, result) -> dict:
        return {"detections": ctx["text"]}

    def reset(self) -> None:
        if self.dry_run:
            return
        self.state(phase="reset")
        r = self.robot.reset()
        if not r.ok:
            self.log.warning("리셋 전달 실패 (%s): %s", r.status, r.detail)
        self.sleep(float(self.config.get("reset_wait_s") or 0))

    def on_command(self, name, args):
        if name == "skip":
            self._skip = True


if __name__ == "__main__":
    raise SystemExit(Recycling.main())
