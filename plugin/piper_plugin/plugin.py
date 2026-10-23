"""`Plugin` — 1층. `setup()` 한 번, `loop()` 반복, `teardown()` 한 번 (feature/plugins.md §7).

런타임이 대신 하는 것: 환경변수 읽기, 설정 기본값, SIGTERM → `teardown` 보장, `loop()` 안의
예외 로그 + 재시도 간격, 상태 KV 의 TTL 갱신, 명령 큐 폴링(`on_command`),
`heartbeat: true` 일 때 데드맨 박동 스레드.
"""

from __future__ import annotations

import logging
import signal
import sys
import threading
import time
from typing import Any

from piper_plugin.api import Api
from piper_plugin.camera import Camera
from piper_plugin.detections import Detections, from_result
from piper_plugin.env import Env
from piper_plugin.robot import Robot


class Topic:
    """버스에서 읽은 토픽 하나 — `data` 가 발행자가 올린 값이다."""

    def __init__(self, raw: dict) -> None:
        self.raw = raw
        self.topic: str = str(raw.get("topic", ""))
        self.publisher: str = str(raw.get("publisher", ""))
        self.seq: int = int(raw.get("seq", 0) or 0)
        self.ts: float = float(raw.get("ts", 0) or 0)
        self.data = raw.get("data")

    def __repr__(self) -> str:
        return f"Topic({self.topic!r} #{self.seq} from {self.publisher!r})"


class PluginStop(Exception):
    """루프를 끝내라. `self.stop("사유")` 가 던진다 — 예외가 아니라 신호다."""


class Plugin:
    #: `loop()` 가 예외로 죽었을 때 다시 부르기 전에 쉬는 시간
    retry_s: float = 1.0
    #: 상태 KV 를 다시 올리는 간격 (TTL 15s 보다 짧게)
    state_refresh_s: float = 5.0
    #: 데드맨 박동 간격 (`resources.heartbeat`)
    heartbeat_s: float = 0.5

    def __init__(self, env: Env | None = None) -> None:
        self.env = env or Env.from_environ()
        self.name = self.env.name
        self.config: dict[str, Any] = dict(self.env.config)
        self.cameras: dict[str, str] = dict(self.env.cameras)
        self.inputs: dict[str, str] = dict(self.env.inputs)
        self.log = logging.getLogger(f"plugin.{self.name}")
        self.api = Api(self.env.api_base, self.env.token)
        self.robot = Robot(self.api)
        self._cams: dict[str, Camera] = {}
        self._bus = None
        self._state: dict[str, Any] = {}
        self._state_pushed = 0.0
        self._stopping = False
        self._stop_reason = ""
        self.started_at = time.time()

    # ── 훅 ──

    def setup(self) -> None:
        """모델 로드 등. 한 번."""

    def loop(self) -> None:
        """반복 호출된다. 반환하면 바로 다시 불린다 — 기다릴 일은 `self.sleep()` 으로."""
        raise NotImplementedError("loop() 를 구현하라")

    def teardown(self) -> None:
        """정지·SIGTERM 때 한 번. 켜 둔 추론을 끄는 자리."""

    def on_command(self, name: str, args: dict) -> None:
        """매니페스트 `commands` 의 버튼이 눌리면 (화면 또는 ext API). 기본은 무시."""
        self.log.info("명령 무시: %s %s", name, args)

    # ── 재료 ──

    def camera(self, key: str) -> Camera:
        """매니페스트 `cameras` 의 슬롯 키, 또는 스트림 이름 그대로."""
        stream = self.cameras.get(key, key)
        cam = self._cams.get(stream)
        if cam is None:
            cam = self._cams[stream] = Camera(stream)
        return cam

    def camera_mapping(self) -> dict[str, str]:
        """`{슬롯 키: 스트림}` — 추론 시작의 `camera_mapping` 에 그대로 넣는다."""
        return dict(self.cameras)

    @property
    def bus(self):
        if self._bus is None:
            from piper_bus.client import Bus

            self._bus = Bus()
        return self._bus

    # ── 플러그인 간 입출력 (feature/plugins.md §7-3) ──

    def input(self, key: str) -> str:
        """매니페스트 `inputs` 의 슬롯 키 → 시작할 때 고른 소스(검출 이름·토픽). 안 골랐으면 키 그대로."""
        return self.inputs.get(key, key)

    def publish(self, topic: str, data, ttl_s: float | None = None) -> dict:
        """토픽 최신값을 올린다 — 다음 플러그인이 `latest()` 로 읽는다. 매니페스트 `outputs.topics` 에 적어 둔다."""
        return self.bus.put_topic(topic, data, publisher=self.name,
                                  ttl_ms=int(ttl_s * 1000) if ttl_s else None)

    def latest(self, topic: str, fresh_s: float | None = None):
        """토픽 최신값(`Topic`) — 없거나 `fresh_s` 보다 낡았으면 None. `topic` 은 슬롯 키여도 된다."""
        raw = self.bus.get_topic(self.input(topic))
        if not raw:
            return None
        t = Topic(raw)
        if fresh_s is not None and time.time() - t.ts > fresh_s:
            return None
        return t

    def input_detections(self, key: str, fresh_s: float | None = None) -> Detections | None:
        """`kind: detections` 입력 슬롯의 최신 검출."""
        return self.detections(self.input(key), fresh_s=fresh_s)

    def detector(self, model: str | None = None, device: str = "cpu"):
        from piper_plugin import detector

        return detector.load(model or detector.DEFAULT_MODEL, device=device)

    def detections(self, name: str, fresh_s: float | None = None) -> Detections | None:
        """버스의 최신 검출(`piper:vision:<name>`). `fresh_s` 보다 낡았으면 None."""
        raw = self.bus.get_detections(name)
        if not raw:
            return None
        d = Detections(raw)
        if fresh_s is not None and time.time() - d.ts > fresh_s:
            return None
        return d

    def publish_detections(self, name: str, shape: tuple[int, int], result=None, *,
                           payload: dict | None = None, seq: int = 0, wall_ns: int | None = None,
                           model: str | None = None) -> dict:
        """검출을 버스에 올린다 — ultralytics 모양의 `result` 또는 완성된 `payload`."""
        if payload is None:
            if result is None:
                raise ValueError("result 나 payload 중 하나는 있어야 한다")
            self._det_seq = getattr(self, "_det_seq", {})
            self._det_seq[name] = self._det_seq.get(name, 0) + 1
            payload = from_result(name, seq, wall_ns if wall_ns is not None else time.time_ns(),
                                  shape, result, det_seq=self._det_seq[name],
                                  publisher=self.name, model=model)
        self.bus.put_detections(name, payload)
        return payload

    def publish_meta(self, meta: dict) -> None:
        """검출 발행자 자기소개 — 버스 `piper:vision_meta:<플러그인 이름>` (feature/plugins.md §13-3).
        루프마다 불러 TTL 을 갱신한다. 화면이 "어느 검출기가 무슨 모델로 도나"를 여기서 읽는다."""
        self.bus.put_vision_meta(self.name, meta)

    def publish_preview(self, name: str, frame_bgr, quality: int = 80) -> None:
        from piper_plugin.image import encode_bgr

        jpeg = encode_bgr(frame_bgr, quality)
        if jpeg:
            self.bus.put_preview(name, jpeg)

    def serve_ui(self, app) -> None:
        """`ui: server` — ASGI 앱을 게이트웨이가 준 소켓에 띄운다(데몬 스레드). 터미널이면 127.0.0.1:8765."""
        from piper_plugin.ui import serve_ui

        serve_ui(app, self.env.ui_socket or None)

    def state(self, **kv) -> None:
        """화면에 보일 상태 KV. 누적된다 — 바뀐 키만 넘겨도 된다."""
        self._state.update(kv)
        self._push_state()

    def judge(self, user: str, system: str | None = None, **kw) -> dict | None:
        """LLM 판단 → 슬롯 dict. 실패(타임아웃·403)는 None 과 로그 — 판단 실패는 회차 스킵의 재료다."""
        r = self.robot.judge(user, system, **kw)
        if not r.ok:
            self.log.warning("판단 실패 (%s): %s", r.status, r.detail or r.text[:200])
            return None
        return r.json()

    def sleep(self, seconds: float) -> None:
        """정지 신호에 바로 깨는 sleep."""
        deadline = time.monotonic() + seconds
        while not self._stopping and time.monotonic() < deadline:
            time.sleep(min(0.1, max(0.0, deadline - time.monotonic())))
        if self._stopping:
            raise PluginStop(self._stop_reason)

    def stop(self, reason: str = "") -> None:
        self._stopping, self._stop_reason = True, reason
        raise PluginStop(reason)

    @property
    def stopping(self) -> bool:
        return self._stopping

    def estopped_since(self, t: float) -> dict | None:
        """`t` 이후 E-stop 이 있었나 — 있으면 그 기록."""
        try:
            last = self.bus.last_estop()
        except Exception:
            return None
        if last and float(last.get("ts", 0) or 0) >= t:
            return last
        return None

    # ── 런타임 ──

    def _push_state(self, force: bool = False) -> None:
        now = time.time()
        if not force and now - self._state_pushed < 0.2:
            return
        try:
            self.bus.put_plugin_state(self.name, {**self._state, "ts": now, "uptime_s": round(now - self.started_at, 1)})
            self._state_pushed = now
        except Exception as e:
            self.log.debug("상태 발행 실패: %s", e)

    def _poll_commands(self) -> None:
        try:
            cmd = self.bus.pop_plugin_command(self.name)
        except Exception:
            return
        while cmd:
            try:
                self.on_command(str(cmd.get("name", "")), dict(cmd.get("args") or {}))
            except PluginStop:
                raise
            except Exception as e:
                self.log.exception("on_command 실패: %s", e)
            cmd = self.bus.pop_plugin_command(self.name)

    def _heartbeat_thread(self) -> threading.Thread:
        def beat():
            while not self._stopping:
                try:
                    self.robot.heartbeat(plugin=self.name)
                except Exception as e:
                    self.log.debug("heartbeat 실패: %s", e)
                time.sleep(self.heartbeat_s)
        t = threading.Thread(target=beat, name="plugin-heartbeat", daemon=True)
        t.start()
        return t

    def _after_setup(self) -> None:
        """하위 클래스(MissionPlugin)가 setup 뒤 검사를 끼우는 자리."""

    def run(self) -> int:
        logging.basicConfig(level=logging.INFO, format=f"%(asctime)s [%(levelname)s] {self.name}: %(message)s",
                            stream=sys.stdout, force=True)

        def _sig(signum, _frame):
            self.log.info("signal %s — 정지", signum)
            self._stopping, self._stop_reason = True, f"signal {signum}"

        signal.signal(signal.SIGTERM, _sig)
        signal.signal(signal.SIGINT, _sig)

        if self.env.heartbeat and self.api.available:
            self._heartbeat_thread()
        code = 0
        try:
            self.setup()
            self._after_setup()
            self.state(phase=self._state.get("phase", "running"))
            while not self._stopping:
                self._poll_commands()
                try:
                    self.loop()
                except PluginStop:
                    break
                except Exception as e:
                    self.log.exception("loop() 실패 — %.1fs 뒤 다시: %s", self.retry_s, e)
                    self.state(error=str(e)[:200])
                    self.sleep(self.retry_s)
                    continue
                if time.time() - self._state_pushed > self.state_refresh_s:
                    self._push_state(force=True)
        except PluginStop:
            pass
        except Exception as e:
            self.log.exception("setup 실패: %s", e)
            code = 1
        finally:
            try:
                self.teardown()
            except Exception as e:
                self.log.exception("teardown 실패: %s", e)
            self._state["phase"] = "stopped"
            if self._stop_reason:
                self._state["stop_reason"] = self._stop_reason
            self._push_state(force=True)
            for cam in self._cams.values():
                cam.close()
        self.log.info("종료%s", f" ({self._stop_reason})" if self._stop_reason else "")
        return code

    @classmethod
    def main(cls, argv: list[str] | None = None) -> int:
        """`if __name__ == "__main__": MyPlugin.main()`."""
        return cls().run()
