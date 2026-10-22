"""`MissionPlugin` — 2층. 관찰 → 판단 → 실행 → 완료 → 기록 → 리셋 (feature/plugins.md §7-1).

오케스트레이터 1단계가 하드코딩했던 루프를 상속 가능한 클래스로 바꾼 것이다(§12).
사용자는 훅만 채운다:

| 훅 | 역할 | 기본 |
|---|---|---|
| `observe() -> ctx` | 한 회차의 입력을 모은다 | `{}` |
| `decide(ctx) -> Decision` | `task(...)` · `skill(...)` · `skip(...)` | **반드시** 구현 |
| `done(ctx) -> bool` | 완료 판정. 대기 중 주기적으로 불린다 | False → 타임아웃으로 끝난다 |
| `record(ctx, result) -> dict` | 저널에 더 남길 것 | `{}` |
| `reset()` · `before_episode()` · `on_estop()` | 그 이름대로 | 비어 있음 |

뼈대가 대신 하는 것: 시작 전 검사(추론이 도는가, 정책이 `task` 를 읽는가 — 아니면 **시작
거부**), `task` 전달과 대기, 회차 수·타임아웃·`dry_run`, E-stop·추론 사망·정지 요청 감시,
판단 실패 시 회차 스킵, JSONL 저널(`<runtime>/runs/<run_id>.jsonl`), 상태 KV.

공통 설정(매니페스트에 없어도 받는다): `max_episodes`(0=무한) · `episode_timeout_s` ·
`settle_s` · `dry_run` · `skills`(`{이름: 체크포인트}` — 언어를 안 읽는 정책용).
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Literal

from piper_plugin.plugin import Plugin, PluginStop


@dataclass
class Decision:
    kind: Literal["task", "skill", "skip"]
    text: str = ""
    reason: str = ""
    extra: dict = field(default_factory=dict)

    @staticmethod
    def task(text: str, **extra) -> "Decision":
        """언어 정책에 줄 지시문."""
        return Decision("task", text=text, extra=extra)

    @staticmethod
    def skill(name: str, **extra) -> "Decision":
        """언어를 안 읽는 정책(ACT 등)용 — `skills` 설정의 체크포인트로 추론을 갈아탄다."""
        return Decision("skill", text=name, extra=extra)

    @staticmethod
    def skip(reason: str = "") -> "Decision":
        """이번 회차는 건너뛴다 (물체 없음, 판단 실패 …)."""
        return Decision("skip", reason=reason)

    def to_dict(self) -> dict:
        return {"kind": self.kind, "text": self.text, "reason": self.reason, **({"extra": self.extra} if self.extra else {})}


COMMON_DEFAULTS = {"max_episodes": 0, "episode_timeout_s": 40.0, "settle_s": 2.0, "dry_run": False, "skills": {}}
COMMON_FIELDS = [
    {"key": "max_episodes", "kind": "number", "default": 0, "min": 0, "max": 10000, "label": "회차 수 (0 = 무한)"},
    {"key": "episode_timeout_s", "kind": "number", "default": 40, "min": 1, "max": 600, "label": "회차 타임아웃(초)"},
    {"key": "settle_s", "kind": "number", "default": 2, "min": 0, "max": 60, "step": 0.5, "label": "회차 사이 대기(초)"},
    {"key": "dry_run", "kind": "bool", "default": False, "label": "드라이런 (로봇 없이 루프만)"},
]


class MissionPlugin(Plugin):
    poll_s: float = 0.5

    def __init__(self, env=None) -> None:
        super().__init__(env)
        for k, v in COMMON_DEFAULTS.items():
            self.config.setdefault(k, v)
        self.episode = 0
        self.run_id = self.env.run_id or time.strftime("run_%Y%m%d_%H%M%S")
        self.journal_path = self.env.runtime_dir / "runs" / f"{self.run_id}.jsonl"
        self._current_skill: str | None = None

    # ── 훅 ──

    def observe(self) -> dict:
        return {}

    def decide(self, ctx: dict) -> Decision:
        raise NotImplementedError("decide(ctx) 를 구현하라")

    def done(self, ctx: dict) -> bool:
        return False

    def record(self, ctx: dict, result: dict) -> dict:
        return {}

    def reset(self) -> None:
        pass

    def before_episode(self) -> None:
        pass

    def on_estop(self, event: dict) -> None:
        pass

    # ── 뼈대 ──

    @property
    def dry_run(self) -> bool:
        return bool(self.config.get("dry_run"))

    def _after_setup(self) -> None:
        """침묵 제거(§12-1) — ACT 에 task 를 줘도 조용히 헛도는 상태로 시작하지 않는다."""
        if self.dry_run:
            self.log.info("드라이런 — 로봇·정책 검사를 건너뛴다")
            return
        if not self.api.available:
            self.stop("게이트웨이 없이 미션을 돌릴 수 없다 (--api 또는 게이트웨이에서 시작)")
        pol = self.robot.policy()
        if not pol or not pol.get("running"):
            self.stop("추론이 돌고 있지 않다 — 먼저 추론을 시작하라 (또는 dry_run)")
        if not pol.get("takes_language") and not self.config.get("skills"):
            self.stop(f"정책 '{pol.get('type')}' 은 task 를 읽지 않는다 — 스킬 표(`skills`)도 없어 판단이 로봇에 닿지 않는다")

    def journal(self, record: dict) -> None:
        self.journal_path.parent.mkdir(parents=True, exist_ok=True)
        with self.journal_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"ts": time.time(), "run": self.run_id, "episode": self.episode, **record},
                               ensure_ascii=False) + "\n")

    def _check_abort(self) -> None:
        ev = self.estopped_since(self.started_at)
        if ev:
            self.on_estop(ev)
            self.stop(f"E-stop ({ev.get('reason', '?')})")
        if not self.dry_run:
            alive = self.robot.inference_running()
            if alive is False:
                self.stop("추론이 멈췄다")

    def wait_until(self, pred, timeout_s: float) -> bool:
        """`pred()` 가 참이 되면 True, 타임아웃이면 False. 기다리는 동안 E-stop·추론 사망·정지 요청을 본다."""
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            self._check_abort()
            try:
                if pred():
                    return True
            except Exception as e:
                self.log.warning("done() 실패 (타임아웃으로 간다): %s", e)
            self.sleep(self.poll_s)
        return False

    def _apply(self, d: Decision) -> bool:
        if self.dry_run:
            self.log.info("[dry_run] %s: %s", d.kind, d.text)
            return True
        if d.kind == "task":
            r = self.robot.set_task(d.text)
        else:
            skills = self.config.get("skills") or {}
            ckpt = skills.get(d.text)
            if not ckpt:
                self.log.error("스킬 '%s' 의 체크포인트가 없다 (skills 설정)", d.text)
                return False
            if self._current_skill == d.text:
                return True
            self.robot.stop_inference()
            r = self.robot.start_inference(ckpt, self.camera_mapping())
            if r.ok:
                self._current_skill = d.text
        if not r.ok:
            self.log.error("실행 전달 실패 (%s): %s", r.status, r.detail or r.text[:200])
        return r.ok

    def loop(self) -> None:
        """회차 하나."""
        max_ep = int(self.config.get("max_episodes") or 0)
        if max_ep and self.episode >= max_ep:
            self.stop(f"{max_ep}회차 완료")
        self._check_abort()
        self.episode += 1
        self.state(phase="observe", episode=self.episode, run_id=self.run_id,
                   max_episodes=max_ep, journal=str(self.journal_path))
        self.before_episode()
        t0 = time.monotonic()

        ctx = self.observe()
        self.state(phase="decide")
        try:
            d = self.decide(ctx)
        except PluginStop:
            raise
        except Exception as e:
            self.log.exception("판단 실패 — 회차 스킵: %s", e)
            d = Decision.skip(f"decide 예외: {e}")
        self.state(decision=d.to_dict())

        if d.kind == "skip":
            self.journal({"decision": d.to_dict(), "result": {"skipped": True}, **self.record(ctx, {"skipped": True})})
            self.state(phase="skip")
            self.sleep(float(self.config.get("settle_s") or 0))
            return

        self.state(phase="execute")
        sent = self._apply(d)
        finished = False
        if sent:
            self.state(phase="wait")
            finished = self.wait_until(lambda: self.done(ctx), float(self.config.get("episode_timeout_s") or 40))
        result = {"sent": sent, "done": finished, "timeout": sent and not finished,
                  "elapsed_s": round(time.monotonic() - t0, 2)}
        self.state(phase="record", last_result=result)
        extra = self.record(ctx, result)
        self.journal({"decision": d.to_dict(), "result": result, **(extra or {})})

        self.state(phase="reset")
        self.reset()
        self.sleep(float(self.config.get("settle_s") or 0))
