"""분리수거 에피소드 오케스트레이터 — 1단계 (feature/episode-orchestrator.md).

문서 원안의 1단계 시나리오는 팝콘이었지만 G1(뎁스 스냅샷)·G3(로드셀 판정)가
하드웨어에 막혀, 준비된 두 칸(yolod·llm_client)으로 **분리수거 최소 루프**를
먼저 세운다 (결정은 문서 머리말에 기록). 판정은 아직 타임아웃이다 — G3 이
오면 wait_done 스텝 하나만 바뀐다.

## 설계 (문서 §1~§4 그대로)

- **취소 가능한 async 상태기계.** 스텝은 하드코딩된 async 함수 — 2단계에서
  레지스트리·YAML 스펙으로 뺄 때 이 함수들이 그대로 스텝 구현체가 된다.
- **스텝은 기존 서비스를 직접 호출한다.** 자기 자신에게 HTTP 를 치지 않는다:
  검출은 버스(`get_detections`), 판단은 `llm_client.judge`, task/reset 은
  `param_bridge.send_params` — wrapper 는 이미 버스로 task 를 실시간 수신한다.
- **전 구간 취소 가능.** 매 대기 구간에서 정지 요청·E-stop(`bus.last_estop`
  변화)·추론 프로세스 사망을 폴링해 즉시 끝낸다. LLM 호출도 태스크 취소에
  함께 죽는다 (llm-integration §3).
- **저널링.** 회차마다 스텝 입출력을 JSONL 로 남긴다 — "실패 원인 3분됨"
  (YOLO/LLM/VLA)의 재료.

## 이 모듈은 exclusivity 를 import 하지 않는다

exclusivity 가 상태 제공자로 이 싱글턴을 import 한다 — 역방향이면 순환이다.
추론 생존 확인은 process_manager 를 직접 본다.
"""

import asyncio
import json
import logging
import time
from collections import deque
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from pydantic import BaseModel, Field, create_model

from app.core.config import settings
from app.services import llm_client
from app.services.llm_client import LLMJudgeError
from app.services.process_manager import ProcessState, process_manager

logger = logging.getLogger(__name__)

# 드라이런·스킵 회차의 짧은 대기 — 테스트가 0 으로 줄인다
DRY_WAIT_S = 2.0

# 분리수거 판단 기본 규칙 — 운영 정본은 프리셋 스토어로 갈 예정 (llm-integration §4).
# `/api/vision` 테스트 페이지와 오케스트레이터가 같은 시드를 쓴다.
DEFAULT_RULES = """너는 로봇 팔의 분리수거 판단기다. 객체 검출 목록을 보고 다음에 집을 물체
하나와 목적지 통을 정한다.

규칙:
- plastic_bin: bottle(페트병), cup(플라스틱 컵)
- can_bin: 캔 종류
- trash_bin: 그 외 쓰레기, 분류 불확실한 것 전부
- 신뢰도가 가장 높은 물체를 우선한다. 사람·가구는 대상이 아니다.
- 집을 물체가 없으면 target="none", destination="none".
- reason 은 한국어 한 문장으로 짧게.
"""


class JudgeSlots(BaseModel):
    """분리수거 판단 슬롯.

    ⚠ Field description 은 장식이 아니다 — `model_json_schema()` 를 타고 로컬
    모델의 guided decoding 에 들어간다. 설명이 없으면 소형 모델이 reason 을
    폭주시키는 것을 실측했다 ("검출 없음" 입력에서 1024토큰 잡탕).
    """

    target: str = Field(description="집을 물체의 라벨 (검출 목록에 있는 것) 또는 'none'")
    destination: str = Field(description="plastic_bin | can_bin | trash_bin | none")
    reason: str = Field(description="판단 근거, 한국어 한 문장")


def slots_model(skills: dict[str, str]) -> type[BaseModel]:
    """이번 회차에 쓸 판단 스키마.

    ⚠ 허용값을 **런타임에** 넣어야 한다. `Field(description=...)` 이 그대로
    guided decoding 스키마로 들어가므로(위 `JudgeSlots` 주석), 등록된 스킬이
    바뀌면 LLM 이 고를 수 있는 값도 같이 바뀌어야 한다. 목록을 코드에 박으면
    가중치를 하나 추가할 때마다 여기를 고쳐야 하고, 안 고치면 **LLM 이 존재하지
    않는 스킬을 고르거나 있는 스킬을 못 고른다.**
    """
    if not skills:
        return JudgeSlots
    allowed = " | ".join(sorted(skills))
    return create_model(
        "JudgeSlotsWithSkill",
        __base__=JudgeSlots,
        skill=(str, Field(description=f"실행할 스킬 ({allowed})")),
    )


class OrchestratorConfig(BaseModel):
    max_episodes: int = Field(10, ge=1, le=200)
    cams: list[str] = []                # 빈 리스트 = 살아 있는 검출 전부
    rules: str = DEFAULT_RULES
    task_template: str = "pick up the {target} and put it in the {destination}"
    wait_s: float = Field(40.0, gt=0)   # 실행 대기 — G3(로드셀) 판정이 오기 전까지의 대용
    reset_wait_s: float = Field(10.0, ge=0)
    freshness_s: float = 3.0            # 검출 신선도 한계
    provider: str | None = None         # 기본: 설정 (이 머신은 Ollama)
    model: str | None = None
    # 로봇 없이 루프 자체를 굴려본다: task/reset 미전송, 대기 단축, 추론 생존 무시
    dry_run: bool = False

    # ── 언어를 안 쓰는 정책(ACT 등)을 위한 스킬 레지스트리 ──
    #
    # `{스킬 이름: 체크포인트 경로}`. VLA 는 판단을 **문장**으로 받지만 ACT 는
    # 문장을 안 읽으므로, 판단이 행동으로 이어지려면 **어느 가중치를 올릴지**로
    # 번역돼야 한다. 비어 있으면 언어 정책 전용 루프다.
    #
    # ⚠ 이름이 곧 LLM 이 고를 수 있는 값이다 — 사람이 읽고 판단할 수 있게 짓는다
    #   ("plastic_bin_pick" 처럼). 경로를 그대로 고르게 하지 않는 이유이기도 하다.
    skills: dict[str, str] = {}


class _Abort(Exception):
    """루프 전체 중단 (정지 요청·E-stop·추론 사망·검출 없음)."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class EpisodeOrchestrator:
    def __init__(self) -> None:
        self.state = "idle"             # idle | running | stopping
        self.episode = 0
        self.max_episodes = 0
        self.current_step = ""
        self.last_event: dict = {}
        self.history: deque[dict] = deque(maxlen=50)
        self.journal_path: Path | None = None
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self._estop_baseline: float | None = None
        self._on_event: Callable[[dict], None] | None = None

    # ── 표면 ──

    @property
    def is_running(self) -> bool:
        return self.state != "idle"

    def set_event_callback(self, cb: Callable[[dict], None]) -> None:
        self._on_event = cb

    def status(self) -> dict:
        return {
            "state": self.state,
            "episode": self.episode,
            "max_episodes": self.max_episodes,
            "step": self.current_step,
            "last_event": self.last_event,
            "history": list(self.history),
            "journal": str(self.journal_path) if self.journal_path else None,
        }

    async def start(self, cfg: OrchestratorConfig) -> None:
        if self.is_running:
            raise RuntimeError("오케스트레이터가 이미 실행 중입니다")
        # 사전 점검 — 루프 안에서 죽는 것보다 시작을 막는 게 낫다
        if not cfg.dry_run and not _inference_busy():
            raise RuntimeError("추론이 실행 중이 아닙니다 — 정책을 먼저 배포하세요 (dry_run 은 예외)")
        if not cfg.dry_run:
            _require_actionable_policy(cfg)
        fresh = await asyncio.to_thread(_fresh_detections, cfg.cams, cfg.freshness_s)
        if not fresh and not cfg.dry_run:
            raise RuntimeError("살아 있는 검출이 없습니다 — 검출기를 먼저 켜세요")

        self.state = "running"
        self.episode = 0
        self.max_episodes = cfg.max_episodes
        self.history.clear()
        self._stop = asyncio.Event()
        journal_dir = settings.log_dir / "orchestrator"
        journal_dir.mkdir(parents=True, exist_ok=True)
        self.journal_path = journal_dir / f"run_{datetime.now():%Y%m%d_%H%M%S}.jsonl"
        self._task = asyncio.create_task(self._run(cfg))

    async def stop(self) -> None:
        if not self.is_running:
            return
        self.state = "stopping"
        self._stop.set()
        if self._task:
            try:
                await asyncio.wait_for(self._task, timeout=10)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                self._task.cancel()

    # ── 루프 ──

    async def _run(self, cfg: OrchestratorConfig) -> None:
        estop = await asyncio.to_thread(_last_estop_at)
        self._estop_baseline = estop
        self._emit({"event": "run_start", "max_episodes": cfg.max_episodes,
                    "dry_run": cfg.dry_run})
        stop_reason = "done"
        try:
            for ep in range(1, cfg.max_episodes + 1):
                self.episode = ep
                await self._episode(cfg, ep)
        except _Abort as e:
            stop_reason = e.reason
        except asyncio.CancelledError:
            stop_reason = "cancelled"
        except Exception:
            logger.exception("오케스트레이터 루프 오류")
            stop_reason = "error"
        finally:
            self.state = "idle"
            self.current_step = ""
            self._emit({"event": "run_end", "reason": stop_reason,
                        "episodes": self.episode})

    async def _episode(self, cfg: OrchestratorConfig, ep: int) -> None:
        record: dict = {"episode": ep, "ts": time.time()}

        # 1. capture — 버스에서 신선한 검출을 모은다
        self._set_step("capture", ep)
        texts = await asyncio.to_thread(_fresh_detections, cfg.cams, cfg.freshness_s)
        if not texts:
            if cfg.dry_run:
                texts = {"(dry)": "검출 없음"}
            else:
                raise _Abort("no_fresh_detections")   # yolod 가 죽었다 — 계속할 수 없다
        detections_text = "\n".join(texts.values())
        record["detections"] = detections_text

        # 2. judge — 실패는 회차 스킵 (LLM 은 단일 장애점이 아니다, llm-integration §3)
        self._set_step("judge", ep)
        try:
            slots = await llm_client.judge(
                cfg.rules, detections_text, slots_model(cfg.skills),
                timeout_s=30.0, provider=cfg.provider, model=cfg.model,
            )
            record["slots"] = slots.model_dump()
        except LLMJudgeError as e:
            record["judge_error"] = f"{e.reason}: {e.detail}"
            record["outcome"] = "judge_failed"
            await self._finish_episode(record)
            await self._wait(DRY_WAIT_S)              # 같은 실패로 즉시 재돌입 방지
            return

        # 3. 집을 것이 없으면 조용히 다음 장면을 기다린다
        if slots.target == "none":
            record["outcome"] = "nothing_to_pick"
            await self._finish_episode(record)
            await self._wait(DRY_WAIT_S)
            return

        # 4. 판단을 행동으로. **정책 종류에 따라 경로가 다르다.**
        #
        #   언어 정책(VLA) : 문장을 버스로 민다 — 재시작 없음
        #   그 외(ACT 등)  : 문장을 안 읽는다. 스킬 → 체크포인트 교체가 필요하다
        self._set_step("set_task", ep)
        task = cfg.task_template.format(target=slots.target, destination=slots.destination)
        record["task"] = task
        if cfg.skills:
            record["skill"] = getattr(slots, "skill", None)
        if not cfg.dry_run:
            if _language_policy():
                await _send_params({"task": task})
            else:
                # ⚠ 여기서 조용히 넘어가면 **판단이 아무 효과가 없다.** 저널에는
                #   매 회차 다른 결정이 쌓이는데 로봇은 같은 동작만 반복한다.
                #   그 침묵이 이 갈래를 만든 이유다.
                raise _Abort("skill_switch_unsupported")

        # 5. wait_done — 아직 타임아웃 판정 (G3 로드셀이 오면 이 스텝만 바뀐다)
        self._set_step("wait_done", ep)
        await self._wait(DRY_WAIT_S if cfg.dry_run else cfg.wait_s, watch_inference=not cfg.dry_run)
        record["outcome"] = "timeout_done"

        # 6. eval 기록
        await self._finish_episode(record)

        # 7. reset — 원점 복귀 + 버퍼 초기화 (wrapper 의 기존 경로 재사용)
        self._set_step("reset", ep)
        if not cfg.dry_run:
            await _send_params({"reset": True})
            await self._wait(cfg.reset_wait_s, watch_inference=True)

    # ── 보조 ──

    def _set_step(self, step: str, ep: int) -> None:
        self.current_step = step
        self._emit({"event": "step", "episode": ep, "step": step})

    def _emit(self, event: dict) -> None:
        self.last_event = {**event, "ts": time.time()}
        if self._on_event:
            try:
                self._on_event(self.last_event)
            except Exception:
                logger.exception("오케스트레이터 이벤트 콜백 오류")

    async def _finish_episode(self, record: dict) -> None:
        self.history.append({k: record.get(k) for k in
                             ("episode", "slots", "task", "outcome", "judge_error")})
        self._emit({"event": "episode_done", **self.history[-1]})
        if self.journal_path:
            line = json.dumps(record, ensure_ascii=False)
            await asyncio.to_thread(_append_line, self.journal_path, line)

    async def _wait(self, seconds: float, *, watch_inference: bool = False) -> None:
        """취소 가능한 대기 — 0.5초마다 정지·E-stop·추론 생존을 본다."""
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if self._stop.is_set():
                raise _Abort("stopped")
            estop = await asyncio.to_thread(_last_estop_at)
            if estop is not None and estop != self._estop_baseline:
                raise _Abort("estop")
            if watch_inference and not _inference_busy():
                raise _Abort("inference_stopped")
            await asyncio.sleep(min(0.5, max(deadline - time.monotonic(), 0.05)))


# ── 바깥 세계 (테스트가 이 선에서 갈아끼운다) ──


def _language_policy() -> bool:
    """지금 도는 정책이 `task` 문장을 실제로 쓰는가.

    모르면(직접 편집한 CLI 로 띄운 경우) **안 쓰는 쪽으로 본다.** 여기서
    낙관하면 판단이 아무 효과 없이 흘러가는데, 그건 화면상 성공으로 보인다.
    """
    from app.core.policies import takes_language
    from app.services import inference_state

    running = inference_state.get()
    return bool(running and running.policy_type and takes_language(running.policy_type))


def _require_actionable_policy(cfg: "OrchestratorConfig") -> None:
    """판단이 **로봇에 닿을 수 있는** 상태인지 시작 전에 본다.

    ⚠ 이 검사가 없던 동안, ACT 를 올려둔 채 루프를 돌리면 LLM 이 매 회차 다른
    결정을 내려도 로봇은 같은 동작만 반복했다. `task` 를 버스로 밀지만 ACT 는
    그 값을 안 읽기 때문이다(`policies.takes_language`). 에러도 경고도 없이
    저널만 그럴듯하게 쌓였다 — **돌아가는 것처럼 보이는 실패**다.
    """
    if _language_policy():
        return
    from app.services import inference_state

    running = inference_state.get()
    what = (running.policy_type if running and running.policy_type else "확인 불가")
    if not cfg.skills:
        raise RuntimeError(
            f"지금 도는 정책({what})은 문장을 읽지 않습니다 — 판단이 로봇에 닿지 "
            "않습니다. 언어 정책(VLA)을 배포하거나, 스킬 레지스트리(skills)를 "
            "설정해 어떤 가중치를 쓸지 정하세요")
    # 스킬은 선언됐지만 교체를 실행할 경로가 아직 없다 (아래 _Abort 와 같은 이유).
    raise RuntimeError(
        f"스킬 {len(cfg.skills)}개가 선언됐지만 체크포인트 교체는 아직 구현되지 "
        "않았습니다 — 교체에는 추론 재시작이 필요합니다")


def _inference_busy() -> bool:
    return process_manager.state not in (ProcessState.IDLE, ProcessState.ERROR)


def _fresh_detections(cams: list[str], freshness_s: float) -> dict[str, str]:
    """카메라별 신선한 검출 텍스트. 낡은 검출로 판단하는 사고를 막는 선이다."""
    from piper_bus.client import Bus

    bus = Bus()
    now = time.time()
    out: dict[str, str] = {}
    for name in bus.detection_names():
        if cams and name not in cams:
            continue
        payload = bus.get_detections(name)
        if payload and now - payload.get("ts", 0) <= freshness_s:
            out[name] = payload.get("text", "")
    return out


def _last_estop_at() -> float | None:
    from piper_bus.client import Bus

    last = Bus().last_estop()
    return last.get("at") if last else None


async def _send_params(params: dict) -> None:
    from app.services.param_bridge import param_bridge

    await param_bridge.send_params(params)


def _append_line(path: Path, line: str) -> None:
    with open(path, "a") as f:
        f.write(line + "\n")


orchestrator = EpisodeOrchestrator()
