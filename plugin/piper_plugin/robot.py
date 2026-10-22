"""`self.robot.*` — 게이트웨이 REST 를 감싼 원시 연산 (feature/plugins.md §7-1 3층).

팔은 **이 길로만** 움직인다. 플러그인은 CAN 도 shm 명령 세그먼트도 직접 잡지 않는다 —
추론(또는 녹화·조그)이 팔을 쥐고, 그 배타·E-stop 규칙은 게이트웨이가 진다.

호출마다 필요한 권한 범위를 docstring 에 적었다. 범위 밖이면 403 `Response` 가 돌아온다.
"""

from __future__ import annotations

from piper_plugin.api import Api, Response


class Robot:
    def __init__(self, api: Api) -> None:
        self.api = api

    # ── 추론 ──

    def start_inference(self, checkpoint_path: str, camera_mapping: dict[str, str], *,
                        policy_type: str = "act", task: str | None = None, **kw) -> Response:
        """추론 시작. 범위 `inference.start`. 409 면 배타(학습 중 등) — `detail` 에 사유."""
        body = {"checkpoint_path": checkpoint_path, "camera_mapping": camera_mapping,
                "policy_type": policy_type, **kw}
        if task is not None:
            body.setdefault("params", {})["task"] = task
        return self.api.post("/api/models/inference/start", json=body)

    def stop_inference(self) -> Response:
        """범위 `inference.stop`."""
        return self.api.post("/api/models/inference/stop")

    def policy(self) -> dict | None:
        """지금 도는 정책 — `{running, type, takes_language, checkpoint_path}`. 범위 `inference.read`.
        모르면(404·오류) None — **모를 때 낙관하지 않는다**(§12-1)."""
        r = self.api.get("/api/models/inference/policy")
        return r.json() if r.ok else None

    def inference_running(self) -> bool | None:
        """`GET /api/activity` 로 본다. 범위 `activity.read`. 못 보면 None."""
        r = self.api.get("/api/activity")
        if not r.ok:
            return None
        d = r.json() or {}
        return "inference" in (d.get("running") or [])

    def activity(self) -> dict | None:
        r = self.api.get("/api/activity")
        return r.json() if r.ok else None

    # ── 실시간 파라미터 (범위 `params.write`) ──

    def set_task(self, task: str) -> Response:
        """언어 정책에 지시문을 전달한다. 재시작 없음 — 에피소드 경계에서 부른다."""
        return self.api.post("/api/params", json={"params": {"task": task}})

    def set_params(self, **params) -> Response:
        return self.api.post("/api/params", json={"params": params})

    def pause(self) -> Response:
        return self.api.post("/api/params/pause")

    def resume(self) -> Response:
        return self.api.post("/api/params/resume")

    def reset(self) -> Response:
        return self.api.post("/api/params/reset")

    # ── 기록·판단·안전 ──

    def eval_log(self, success: bool, checkpoint: str = "", memo: str = "", **kw) -> Response:
        """회차 결과를 평가 기록에 남긴다. 범위 `eval.write`."""
        return self.api.post("/api/eval/log", json={"success": success, "checkpoint": checkpoint,
                                                    "memo": memo, **kw})

    def judge(self, user: str, system: str | None = None, timeout_s: float = 60.0, **kw) -> Response:
        """LLM 판단 (슬롯 JSON). 범위 `vision.judge`. 본문: `{"slots": {...}, ...}`."""
        body = {"user": user, "timeout_s": timeout_s, **kw}
        if system is not None:
            body["system"] = system
        return self.api.post("/api/vision/judge", json=body, timeout_s=timeout_s + 5)

    def estop(self) -> Response:
        """긴급 정지. 열린 경로 — 범위가 필요 없다."""
        return self.api.post("/api/estop/trigger")

    def heartbeat(self, **info) -> Response:
        """데드맨 박동. 열린 경로."""
        return self.api.post("/api/estop/heartbeat", json={"via": "plugin", **info}, timeout_s=3)
