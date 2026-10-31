#!/usr/bin/env python3
"""apple-sort — 작업판 위 사과를 검출해, 남은 종류에 맞는 지시문(task)을 언어 정책에 넣는다.

`dualarm_sort_apple_stage` 로 학습한 SmolVLA-S 는 에피소드 하나에 사과 하나를 옮겼고, 어느
종류인지는 지시문이 정했다("put the red apple in the left box"). 추론 중에 사람이 화면에서
지시문을 바꿔 주던 일을 `MissionPlugin` 훅으로 대신한다:

    observe  팔이 쉬고(관절 정지 + 그리퍼 닫힘) 판 위 개수가 `still_s` 동안 그대로일 때 센다
    decide   규칙 순서대로, 판에 남은 첫 종류의 지시문
    execute  set_task — 같은 종류가 남았으면 같은 문장이다 (정책이 다음 것을 집는다)
    done     판 위 개수가 바뀌고 팔이 다시 쉬면 — 옮기는 도중에는 끝나지 않는다
    record   전후 개수·목표 종류를 저널에 (`.runtime/runs/*.jsonl`)

**지시문은 팔이 쉬는 동안에만 바뀐다.** 잡는 도중에 문장이 바뀌는 건 학습에 없던 관측이다.
종류가 바닥나도 정책은 가만히 있는다 — 데이터셋의 `empty_target` 변형(15개)이 그렇게 가르쳤다
— 그 쉼을 보고 다음 종류로 넘어간다. 같은 종류가 `max_fail` 번 연속 타임아웃이면 그 판에서는
다음 종류를 먼저 한다 (판 위 개수가 바뀌면 다시 센다).

쉼 판정 숫자는 데이터셋 실측이다 (2026-10-31, phase 라벨 IDLE·DONE vs 나머지, 1초 창):
관절 변동 중앙값 0 / 움직임 하위 5% 6.6 → `still_tol` 2, 그리퍼 쉴 때 <1 · 잡고 있을 때 ≥28
→ `grip_closed` 10.

검출 모델은 검출 데이터셋 `dualarm_sort_apple`(에피소드 위 카메라 프레임을 자동 라벨하고
시나리오 계획의 판 위 개수로 검증 — `tools/autolabel.py`)로 학습한 RT-DETR 이다. 라벨 규칙이
"작업판 위 사과만"이라 상자 안 사과는 원래 안 잡힌다. ROI 는 이중 안전선이다.
`dualarm_sort_apple-1031-0501` 은 상자·순위는 맞는데 **점수가 낮다** — 참 검출 0.09~0.2,
잡음 ≤0.07 (학습 루프가 mAP50 포화 뒤 19에폭을 남겼고 새 헤드가 덜 자랐다). 그래서 conf 0.08 +
클래스 무관 NMS 다. 그 설정으로 라벨된 587 프레임의 판 위 개수 586 이 맞았고(학습에 쓴 프레임이라
낙관치), 라벨 못 한 13 장 중 틀린 7 장은 전부 팔이 사과를 가리거나 든 장면이다 — 쉴 때만 세므로
판단에는 안 걸린다. 모델을 바꾸면 conf 부터 다시 잰다 (`tools/replay.py`·`tools/autolabel.py`).

추론은 먼저 켜 둔다 — **학습한 세 문장 중 하나로** (아무 문장이나 넣으면 첫 쉼까지 정책이
무엇을 할지 모른다). 팔이 원위치에 있을 때 이 플러그인을 켠다.

    piper-plugin run plugins/apple-sort --camera top=<위 카메라 스트림> \
        --arm left=<CAN> --arm right=<CAN> --api http://127.0.0.1:8000 [--dry-run]
"""

from __future__ import annotations

import time
from collections import deque
from pathlib import Path

from piper_plugin import Decision, MissionPlugin


def parse_rules(text: str) -> list[tuple[str, str]]:
    """`클래스 = 지시문` 줄들 → `[(클래스, 지시문)]` — 위 줄이 먼저다. `#` 줄은 주석."""
    rules = []
    for line in str(text).splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        label, sep, task = line.partition("=")
        if not sep or not label.strip() or not task.strip():
            raise ValueError(f"규칙 줄은 `클래스 = 지시문` 모양이어야 한다: {line!r}")
        rules.append((label.strip(), task.strip()))
    return rules


def parse_roi(text: str) -> tuple[float, float, float, float]:
    parts = [float(v) for v in str(text).split(",")]
    if len(parts) != 4:
        raise ValueError("roi 는 x1,y1,x2,y2 네 값 (0~1)")
    x1, y1, x2, y2 = parts
    return min(x1, x2), min(y1, y2), max(x1, x2), max(y1, y2)


def resolve_model(name: str, plugin_dir: Path) -> str:
    """경로·HF id 는 그대로. 커스텀 모델 이름(`dualarm_sort_apple-1031-0501`)은 게이트웨이의
    `detector_models_dir` 에서 찾는다 — 설치본(`data/plugins/<이름>`)·저장소 실행(`plugins/<이름>`) 둘 다."""
    p = Path(name).expanduser()
    if p.is_dir():
        return str(p)
    if "/" not in name:
        root = plugin_dir.resolve().parent.parent
        for base in (root / "detector_models", root / "backend" / "data" / "detector_models"):
            if (base / name).is_dir():
                return str(base / name)
    return name


class AppleSort(MissionPlugin):
    poll_s = 0.3

    def setup(self):
        self.rules = parse_rules(self.config["rules"])
        if not self.rules:
            self.stop("규칙이 비었다 — `클래스 = 지시문` 을 한 줄 이상 적어라")
        self.roi = parse_roi(self.config["roi"])
        self.model_path = resolve_model(str(self.config["model"]), self.env.dir)
        self.model_name = Path(self.model_path).name
        self.model = self.detector(self.model_path, device=self.config["device"])
        known = set(self.model.names.values())
        missing = [c for c, _ in self.rules if c not in known]
        if missing:
            self.stop(f"검출 모델에 없는 클래스: {missing} — 모델 클래스: {sorted(known)}")
        self.cam = self.camera("top")
        self.arm_keys = [k for k in ("left", "right") if self.arms.get(k)]
        if not self.arm_keys:
            self.stop("팔 슬롯(left)을 골라라 — 팔이 쉬는지 모르면 지시문을 바꿀 때를 모른다")
        # (시각, 관절 벡터, 그리퍼 최대, 판 위 개수) — `still_s` 창
        self._window: deque = deque()
        self.counts: dict[str, int] = {}
        self.task: str | None = None
        self.target: str | None = None
        self.fails: dict[str, int] = {}
        self._fail_board: dict[str, int] | None = None
        self._empty_seen = 0
        self._timed_out = False
        self._skip = False
        self.state(rules=[c for c, _ in self.rules], model=self.model_name, arms=self.arm_keys)

    # ── 보기 ──

    def _look(self) -> dict[str, int] | None:
        """프레임 한 장 → 판 위 개수. 검출·미리보기를 버스에 올린다."""
        got = self.cam.read_new(timeout_s=1.0)
        if got is None:
            self.state(error="위 카메라 프레임이 안 온다")
            return None
        frame, seq, wall_ns = got
        h, w = frame.shape[:2]
        r = self.model.predict(frame, conf=float(self.config["conf"]), device=self.config["device"], verbose=False)[0]
        r.boxes = self._one_per_apple(r.boxes)
        self.publish_detections(self.name, (h, w), r, seq=seq, wall_ns=wall_ns, model=self.model_name)
        x1, y1, x2, y2 = self.roi
        counts = {c: 0 for c, _ in self.rules}
        for (bx1, by1, bx2, by2), k in zip(r.boxes.xyxy.tolist(), r.boxes.cls.int().tolist()):
            label = r.names[int(k)]
            if label in counts and x1 <= (bx1 + bx2) / 2 / w <= x2 and y1 <= (by1 + by2) / 2 / h <= y2:
                counts[label] += 1
        self._preview(r.plot(), counts)
        return counts

    @staticmethod
    def _one_per_apple(boxes):
        """클래스 무관 NMS — 한 사과에 `red_apple`·`small_apple` 상자가 겹쳐 나오면 점수 높은 것만.
        클래스별 NMS 는 이걸 못 지운다(클래스가 다르니까) — 그러면 한 사과가 두 종류로 세어진다."""
        import torch
        import torchvision

        if len(boxes.conf) == 0:
            return boxes
        xyxy, conf, cls = (torch.as_tensor(v) for v in (boxes.xyxy, boxes.conf, boxes.cls))
        keep = torchvision.ops.nms(xyxy.float(), conf.float(), 0.5)
        return type(boxes)(xyxy[keep], conf[keep], cls[keep])

    def _preview(self, canvas, counts: dict[str, int]) -> None:
        import cv2

        h, w = canvas.shape[:2]
        x1, y1, x2, y2 = self.roi
        cv2.rectangle(canvas, (int(x1 * w), int(y1 * h)), (int(x2 * w), int(y2 * h)), (0, 200, 0), 1)
        lines = [" ".join(f"{k}:{v}" for k, v in counts.items()), f"task: {self.task or '-'}"]
        for i, text in enumerate(lines):
            org = (8, h - 12 - 20 * (len(lines) - 1 - i))
            cv2.putText(canvas, text, org, cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 0, 0), 3, cv2.LINE_AA)
            cv2.putText(canvas, text, org, cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 255, 255), 1, cv2.LINE_AA)
        self.publish_preview(f"det_{self.name}", canvas)

    def _arm_pose(self) -> tuple[list[float], float] | None:
        """(관절 벡터, 그리퍼 최대) — 팔 하나라도 상태가 없거나 낡았으면 None (모르면 쉰다고 보지 않는다)."""
        joints: list[float] = []
        grip = 0.0
        for k in self.arm_keys:
            s = self.arm(k).read()
            if s is None or not s.fresh(1.0):
                return None
            joints += [v for n, v in sorted(s.joints.items()) if n != "gripper"]
            grip = max(grip, s.gripper)
        return joints, grip

    def _resting(self) -> bool:
        """한 번 보고 창에 넣는다. 창이 `still_s` 를 덮고, 그동안 관절 변동 < `still_tol`,
        그리퍼 닫힘, 판 위 개수 불변이면 쉰다."""
        counts = self._look()
        arms = self._arm_pose()
        now = time.monotonic()
        if counts is None or arms is None:
            self._window.clear()
            self.state(resting=False)
            return False
        self.counts = counts
        self._window.append((now, arms[0], arms[1], counts))
        span = float(self.config["still_s"])
        while len(self._window) > 1 and now - self._window[1][0] >= span:
            self._window.popleft()
        tol = float(self.config["still_tol"])
        rest = (now - self._window[0][0] >= span
                and all(c == counts for *_, c in self._window)
                and all(g <= float(self.config["grip_closed"]) for _, _, g, _ in self._window)
                and all(max(col) - min(col) < tol for col in zip(*(j for _, j, _, _ in self._window))))
        self.state(resting=rest, counts=counts)
        return rest

    # ── 훅 ──

    def observe(self) -> dict:
        self._window.clear()
        settled = self.wait_until(self._resting, float(self.config["episode_timeout_s"]))
        return {"counts": dict(self.counts) if settled else None}

    def decide(self, ctx) -> Decision:
        board = ctx["counts"]
        if board is None:
            return Decision.skip("not_resting")                 # 팔이 안 쉰다 — 다음 회차에 다시 본다
        if board != self._fail_board:                           # 판이 바뀌었다 — 실패 기록은 옛 판 것
            self.fails.clear()
        if not any(board.values()):
            self._empty_seen += 1
            if self._empty_seen >= 2:                           # 두 번 연속 비었을 때만 — 한 번은 오검출일 수 있다
                self._finish("판 위 사과를 모두 옮겼다")
            return Decision.skip("board_empty")
        self._empty_seen = 0
        for label, task in self.rules:
            if board.get(label, 0) > 0 and self.fails.get(label, 0) < int(self.config["max_fail"]):
                if task != self.task:
                    self.log.info("지시문 → %r (판 위 %s)", task, board)
                self.task, self.target = task, label
                self.state(task=task)
                return Decision.task(task, target=label, counts=board)
        self._finish(f"남은 사과가 있는데 모든 종류가 연속 {self.config['max_fail']}번 실패했다 — 사람이 볼 차례")

    def done(self, ctx) -> bool:
        if self._skip:                                          # [skip] 버튼
            self._skip = False
            return True
        return self._resting() and self.counts != ctx["counts"]

    def record(self, ctx, result) -> dict:
        before, after = ctx["counts"], dict(self.counts)
        self._timed_out = bool(result.get("timeout"))
        rec = {"target": self.target, "task": self.task, "counts_before": before}
        if result.get("done"):
            delta = {k: after.get(k, 0) - before.get(k, 0) for k in before}
            rec.update(counts_after=after, delta=delta,
                       picked_target=delta.get(self.target) == -1
                       and all(v == 0 for k, v in delta.items() if k != self.target))
            if not rec["picked_target"]:
                self.log.warning("판이 지시와 다르게 바뀌었다: %s (목표 %s)", delta, self.target)
        elif self._timed_out and self.target:
            self.fails[self.target] = self.fails.get(self.target, 0) + 1
            self._fail_board = before
            self.log.warning("%s 타임아웃 (%d번째 연속)", self.target, self.fails[self.target])
        return rec

    def reset(self) -> None:
        if self._timed_out and self.config.get("reset_on_timeout") and not self.dry_run:
            r = self.robot.reset()
            self.log.info("타임아웃 — 리셋 (%s)", r.status)

    def on_command(self, name, args):
        if name == "skip":
            self._skip = True

    def _finish(self, reason: str) -> None:
        act = self.config.get("on_finish")
        if not self.dry_run:
            if act == "pause":
                self.robot.pause()
            elif act == "stop_inference":
                self.robot.stop_inference()
        self.stop(reason)


if __name__ == "__main__":
    raise SystemExit(AppleSort.main())
