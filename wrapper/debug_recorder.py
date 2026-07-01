"""
추론 디버그 레코더.

디버그 모드에서 추론 머신(정책)과 주고받은 모든 데이터를 별도 폴더에 통째로 남긴다.
디스크 I/O는 전부 백그라운드 워커 스레드에서 처리하므로 실시간 제어 루프를 막지 않는다.

로컬(lerobot_wrapper.py)/서버(grpc_wrapper.py) 두 경로가 공유한다.
wrapper 디렉터리가 이미 sys.path에 있어 `from debug_recorder import DebugRecorder`로 임포트.

폴더 레이아웃 ($PIPER_DEBUG_DIR/{ts}/):
  meta.json            모드, args, 모터명, 카메라 등
  observations.jsonl   정책에 보낸 관측(상태 + task + 이미지 파일명)
  inference.jsonl      정책이 되돌려준 raw action chunk + 후처리
  control.jsonl        스텝별 target/filtered/actual/fps
  images/{cam}/{seq:06d}.jpg
"""

from __future__ import annotations

import datetime
import json
import logging
import os
import queue
import threading
import time
from pathlib import Path
from typing import Any

import numpy as np

logger = logging.getLogger(__name__)

# 워커 큐 최대 길이. 초과 시 가장 오래된 항목을 드롭(제어 루프는 절대 블로킹하지 않음).
_QUEUE_MAXSIZE = 120


def _jsonable(o: Any) -> Any:
    """numpy/tuple 등을 JSON 직렬화 가능한 형태로 변환."""
    if isinstance(o, np.ndarray):
        return o.tolist()
    if isinstance(o, (np.floating, np.integer)):
        return o.item()
    if isinstance(o, (list, tuple)):
        return [_jsonable(x) for x in o]
    if isinstance(o, dict):
        return {str(k): _jsonable(v) for k, v in o.items()}
    return o


def _safe(name: str) -> str:
    """카메라 이름을 폴더명으로 안전하게."""
    return str(name).replace(os.sep, "_").replace(" ", "_").replace("/", "_")


class DebugRecorder:
    """추론 중 주고받은 모든 데이터를 백그라운드로 기록."""

    def __init__(self, mode: str, meta: dict | None = None) -> None:
        base = os.environ.get("PIPER_DEBUG_DIR", "/tmp/piper_debug")
        ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        self.dir = str(Path(base) / ts)
        os.makedirs(os.path.join(self.dir, "images"), exist_ok=True)

        self._closed = False
        self._dropped = 0
        self._made_dirs: set[str] = set()

        # 단일 라이터(워커)만 기록 → 파일 핸들은 __init__에서 열고 close()에서 닫음
        self._obs_f = open(os.path.join(self.dir, "observations.jsonl"), "w")
        self._inf_f = open(os.path.join(self.dir, "inference.jsonl"), "w")
        self._ctl_f = open(os.path.join(self.dir, "control.jsonl"), "w")

        meta_out = {"mode": mode, "created": ts, **(meta or {})}
        try:
            Path(self.dir, "meta.json").write_text(json.dumps(_jsonable(meta_out), indent=2))
        except Exception:
            pass

        self._q: "queue.Queue" = queue.Queue(maxsize=_QUEUE_MAXSIZE)
        self._worker = threading.Thread(target=self._run, name="debug-recorder", daemon=True)
        self._worker.start()
        logger.info("Debug recorder: %s", self.dir)

    # ── public API (제어 루프에서 호출, 비차단) ──

    def record_observation(self, seq: int, obs: dict, task: str | None) -> None:
        """정책에 보낸 관측을 기록. 이미지 numpy는 워커로 넘기기 전에 복사."""
        if self._closed:
            return
        state: dict = {}
        img_files: dict = {}
        img_payload: dict = {}
        for k, v in obs.items():
            if isinstance(v, (int, float)):
                state[k] = float(v)
            elif isinstance(v, np.ndarray) and v.ndim == 3:
                fname = os.path.join("images", _safe(k), f"{seq:06d}.jpg")
                img_files[k] = fname
                img_payload[fname] = v.copy()
        line = {"seq": seq, "t": time.time(), "task": task, "state": state, "images": img_files}
        self._enqueue(("obs", {"line": line, "images": img_payload}))

    def record_inference(
        self, seq: int, action_chunk: Any, postprocessed: Any = None, inference_ms: float | None = None
    ) -> None:
        """정책이 되돌려준 원본 action chunk(+후처리)를 기록."""
        if self._closed:
            return
        line = {
            "seq": seq,
            "t": time.time(),
            "inference_ms": inference_ms,
            "action_chunk": _jsonable(action_chunk),
        }
        if postprocessed is not None:
            line["postprocessed"] = _jsonable(postprocessed)
        self._enqueue(("line", (self._inf_f, line)))

    def record_step(
        self,
        step: int,
        target: Any,
        filtered: Any,
        actual: Any,
        task: str | None,
        paused: bool,
        fps: float,
    ) -> None:
        """스텝별 실행 데이터(target/filtered/actual)를 기록."""
        if self._closed:
            return
        line = {
            "step": step,
            "t": time.time(),
            "fps": fps,
            "task": task,
            "paused": bool(paused),
            "target": _jsonable(target),
            "filtered": _jsonable(filtered),
            "actual": _jsonable(actual),
        }
        self._enqueue(("line", (self._ctl_f, line)))

    def close(self) -> str:
        """큐 flush 후 워커 종료, 파일 닫기. 기록 폴더 경로 반환."""
        if self._closed:
            return self.dir
        self._closed = True
        try:
            self._q.put(None, timeout=2.0)  # 종료 sentinel (드롭 금지)
        except queue.Full:
            pass
        self._worker.join(timeout=10.0)
        for f in (self._obs_f, self._inf_f, self._ctl_f):
            try:
                f.close()
            except Exception:
                pass
        if self._dropped:
            logger.warning("Debug recorder dropped %d records (backpressure)", self._dropped)
        return self.dir

    # ── internal ──

    def _enqueue(self, item: tuple) -> None:
        """비차단 enqueue. 큐가 가득 차면 가장 오래된 항목 드롭."""
        try:
            self._q.put_nowait(item)
        except queue.Full:
            try:
                self._q.get_nowait()
                self._dropped += 1
            except queue.Empty:
                pass
            try:
                self._q.put_nowait(item)
            except queue.Full:
                pass

    def _run(self) -> None:
        import cv2

        while True:
            item = self._q.get()
            try:
                if item is None:
                    break
                kind, payload = item
                if kind == "obs":
                    for fname, img in payload["images"].items():
                        path = os.path.join(self.dir, fname)
                        d = os.path.dirname(path)
                        if d not in self._made_dirs:
                            os.makedirs(d, exist_ok=True)
                            self._made_dirs.add(d)
                        cv2.imwrite(path, cv2.cvtColor(img, cv2.COLOR_RGB2BGR))
                    self._obs_f.write(json.dumps(payload["line"]) + "\n")
                    self._obs_f.flush()
                elif kind == "line":
                    f, line = payload
                    f.write(json.dumps(line) + "\n")
                    f.flush()
            except Exception as e:
                logger.debug("Debug recorder write error: %s", e)
            finally:
                self._q.task_done()
