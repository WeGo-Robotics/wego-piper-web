"""분리수거 오케스트레이터 — 상태기계 계약 (로봇·버스·LLM 없이).

바깥 세계는 서비스 하단의 seam 함수들(_fresh_detections/_send_params/…)로
갈아끼운다 — 루프의 순서·폴백·중단 조건만 본다.
"""

import asyncio
import json

import pytest

from app.core.config import settings
from app.services import llm_client, orchestrator as orch_mod
from app.services.llm_client import LLMJudgeError
from app.services.orchestrator import (
    EpisodeOrchestrator,
    JudgeSlots,
    OrchestratorConfig,
)


@pytest.fixture(autouse=True)
def _world(monkeypatch, tmp_path):
    """기본 세계: 신선한 검출 1개, 판단 성공, E-stop 없음, 추론 살아 있음."""
    monkeypatch.setattr(orch_mod, "DRY_WAIT_S", 0.01)
    monkeypatch.setattr(settings, "log_dir", tmp_path)
    monkeypatch.setattr(orch_mod, "_fresh_detections",
                        lambda cams, fresh: {"top": "[top] bottle(0.91) center=(420,260)"})
    monkeypatch.setattr(orch_mod, "_last_estop_at", lambda: None)
    monkeypatch.setattr(orch_mod, "_inference_busy", lambda: True)
    # ⚠ **무엇이 도는지도 정해야 한다.** 루프가 판단을 `task` 문장으로 내보내는데
    #   ACT 는 그 문장을 안 읽는다 — 이 픽스처는 언어 정책(VLA)을 상정한다.
    #   안 정하면 "확인 불가" 로 잡혀 시작이 막힌다 (그게 새 가드의 요점이다).
    monkeypatch.setattr(orch_mod, "_language_policy", lambda: True)

    sent: list[dict] = []

    async def fake_send(params):
        sent.append(params)

    monkeypatch.setattr(orch_mod, "_send_params", fake_send)

    async def fake_judge(system, user, schema, *, timeout_s, provider, model):
        return JudgeSlots(target="bottle", destination="plastic_bin", reason="테스트")

    monkeypatch.setattr(llm_client, "judge", fake_judge)
    yield sent


def _cfg(**over) -> OrchestratorConfig:
    base = dict(max_episodes=2, wait_s=0.05, reset_wait_s=0.02, dry_run=False)
    base.update(over)
    return OrchestratorConfig(**base)


async def _run_to_end(o: EpisodeOrchestrator, cfg: OrchestratorConfig) -> None:
    await o.start(cfg)
    await asyncio.wait_for(o._task, timeout=10)


def test_full_loop_pushes_task_and_reset(_world):
    o = EpisodeOrchestrator()
    asyncio.run(_run_to_end(o, _cfg()))

    assert o.state == "idle"
    assert o.last_event["event"] == "run_end"
    assert o.last_event["reason"] == "done"
    assert len(o.history) == 2
    assert o.history[0]["outcome"] == "timeout_done"
    assert o.history[0]["task"] == "pick up the bottle and put it in the plastic_bin"
    # 회차마다 task + reset (순서 보존)
    assert _world == [{"task": "pick up the bottle and put it in the plastic_bin"},
                      {"reset": True}] * 2


def test_journal_records_each_episode(_world):
    o = EpisodeOrchestrator()
    asyncio.run(_run_to_end(o, _cfg(max_episodes=1)))

    lines = [json.loads(line) for line in o.journal_path.read_text().splitlines()]
    assert len(lines) == 1
    assert lines[0]["slots"]["target"] == "bottle"
    assert "bottle(0.91)" in lines[0]["detections"]


def test_dry_run_never_touches_the_bus_params(_world):
    o = EpisodeOrchestrator()
    asyncio.run(_run_to_end(o, _cfg(dry_run=True)))
    assert _world == []                      # task/reset 미전송
    assert len(o.history) == 2


def test_judge_failure_skips_episode_not_the_run(_world, monkeypatch):
    calls = {"n": 0}

    async def flaky_judge(*a, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            raise LLMJudgeError("timeout", "느림")
        return JudgeSlots(target="cup", destination="plastic_bin", reason="ok")

    monkeypatch.setattr(llm_client, "judge", flaky_judge)
    o = EpisodeOrchestrator()
    asyncio.run(_run_to_end(o, _cfg()))

    assert [h["outcome"] for h in o.history] == ["judge_failed", "timeout_done"]
    assert o.history[0]["judge_error"] == "timeout: 느림"
    assert len(_world) == 2                  # 실패 회차는 task 를 안 보낸다


def test_none_target_skips_quietly(_world, monkeypatch):
    async def none_judge(*a, **k):
        return JudgeSlots(target="none", destination="none", reason="비었음")

    monkeypatch.setattr(llm_client, "judge", none_judge)
    o = EpisodeOrchestrator()
    asyncio.run(_run_to_end(o, _cfg()))

    assert all(h["outcome"] == "nothing_to_pick" for h in o.history)
    assert _world == []


def test_estop_aborts_mid_wait(_world, monkeypatch):
    ticks = iter([None, None, 123.4])        # 시작 기준선 None → 대기 중 발생

    monkeypatch.setattr(orch_mod, "_last_estop_at",
                        lambda: next(ticks, 123.4))
    o = EpisodeOrchestrator()
    asyncio.run(_run_to_end(o, _cfg(wait_s=5.0)))

    assert o.last_event["reason"] == "estop"
    assert o.state == "idle"


def test_inference_death_aborts(_world, monkeypatch):
    alive = iter([True, True, False])        # 사전점검·초반 통과 후 사망
    monkeypatch.setattr(orch_mod, "_inference_busy", lambda: next(alive, False))
    o = EpisodeOrchestrator()
    asyncio.run(_run_to_end(o, _cfg(wait_s=5.0)))
    assert o.last_event["reason"] == "inference_stopped"


def test_stop_cancels_the_wait(_world):
    async def main():
        o = EpisodeOrchestrator()
        await o.start(_cfg(wait_s=30.0))
        await asyncio.sleep(0.1)
        await o.stop()
        return o

    o = asyncio.run(main())
    assert o.state == "idle"
    assert o.last_event["reason"] == "stopped"


def test_start_guards(_world, monkeypatch):
    async def main():
        o = EpisodeOrchestrator()
        # 추론 없이 실기 시작 금지
        monkeypatch.setattr(orch_mod, "_inference_busy", lambda: False)
        with pytest.raises(RuntimeError, match="추론"):
            await o.start(_cfg())
        # 검출 없이 실기 시작 금지
        monkeypatch.setattr(orch_mod, "_inference_busy", lambda: True)
        # 판단이 로봇에 닿지 않는 정책이면 시작 금지 — 이 가드가 없던 동안
        # ACT 위에서 루프가 돌면 결정이 아무 효과 없이 저널만 쌓였다
        monkeypatch.setattr(orch_mod, "_language_policy", lambda: False)
        with pytest.raises(RuntimeError, match="닿지 않습니다"):
            await o.start(_cfg())
        monkeypatch.setattr(orch_mod, "_language_policy", lambda: True)
        monkeypatch.setattr(orch_mod, "_fresh_detections", lambda c, f: {})
        with pytest.raises(RuntimeError, match="검출"):
            await o.start(_cfg())
        # 드라이런은 둘 다 없어도 된다
        await o.start(_cfg(dry_run=True))
        await asyncio.wait_for(o._task, timeout=10)
        # 이미 실행 중이면 거부
        await o.start(_cfg(dry_run=True))
        with pytest.raises(RuntimeError, match="이미"):
            await o.start(_cfg(dry_run=True))
        await o.stop()

    asyncio.run(main())
