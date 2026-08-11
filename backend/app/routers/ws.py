import asyncio
import json
import re

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app.core import ws_messages as M
from app.services.estop_bridge import estop_bridge
from app.services.process_manager import ProcessState, process_manager
from app.services.training import train_manager
from app.services.record_manager import record_manager
from app.services.policy_server_manager import policy_server_manager

router = APIRouter()

_clients: set[WebSocket] = set()

# robot_client.py 로그에서 텔레메트리 추출용 정규식
_RE_FPS = re.compile(r"Avg FPS:\s*([\d.]+)")
_RE_OBS = re.compile(r"Sent observation #(\d+)")
_RE_LOOP = re.compile(r"Control loop \(ms\):\s*([\d.]+)")
_RE_ACTION = re.compile(r"Action #(\d+) performed")

# 서버 모드 텔레메트리 상태
_server_telemetry = {"step": 0, "fps": 0.0, "inference_ms": 0.0}


async def broadcast(message: dict) -> None:
    for client in list(_clients):
        try:
            await client.send_json(message)
        except Exception:
            _clients.discard(client)


def _setup_callbacks() -> None:
    loop = asyncio.get_running_loop()

    def on_log(line: str) -> None:
        # stderr 로그: [STDERR] 접두사 제거 후 처리
        is_stderr = line.startswith("[STDERR] ")
        clean_line = line[9:] if is_stderr else line

        # 1. wrapper 텔레메트리 JSON
        if clean_line.startswith("{"):
            try:
                data = json.loads(clean_line)
                msg_type = data.get("t")
                if msg_type == "telemetry":
                    asyncio.run_coroutine_threadsafe(
                        broadcast({"type": M.TELEMETRY, "data": data}), loop
                    )
                    return
                if msg_type == "log_saved":
                    asyncio.run_coroutine_threadsafe(
                        broadcast({"type": M.LOG_SAVED, "data": data}), loop
                    )
                    return
            except json.JSONDecodeError:
                pass

        # 2. robot_client.py 로그에서 텔레메트리 추출
        m = _RE_FPS.search(clean_line)
        if m:
            _server_telemetry["fps"] = float(m.group(1))

        m = _RE_OBS.search(clean_line)
        if m:
            _server_telemetry["step"] = int(m.group(1))

        m = _RE_LOOP.search(clean_line)
        if m:
            _server_telemetry["inference_ms"] = float(m.group(1))

        # FPS가 파싱되면 텔레메트리 브로드캐스트
        if _RE_FPS.search(clean_line) or _RE_OBS.search(clean_line):
            telemetry = {
                "t": "telemetry",
                "step": _server_telemetry["step"],
                "fps": _server_telemetry["fps"],
                "inference_ms": _server_telemetry["inference_ms"],
                "joints": [],
                "action": [],
                "task": "",
            }
            asyncio.run_coroutine_threadsafe(
                broadcast({"type": M.TELEMETRY, "data": telemetry}), loop
            )

        # 3. 일반 로그로 전송 (접두사 없이)
        asyncio.run_coroutine_threadsafe(
            broadcast({"type": M.LOG, "data": clean_line}), loop
        )

    def on_state_change(state: ProcessState) -> None:
        # estopd 가 죽일 PID 를 갱신한다 (추론 프로세스가 뜨고 질 때마다)
        estop_bridge.sync_activities()
        asyncio.run_coroutine_threadsafe(
            broadcast({"type": M.STATE, "data": state.value}), loop
        )

    process_manager.set_log_callback(on_log)
    process_manager.set_state_callback(on_state_change)

    # ── 학습 콜백 ──
    def on_train_log(line: str) -> None:
        asyncio.run_coroutine_threadsafe(
            broadcast({"type": M.TRAIN_LOG, "data": line}), loop
        )

    def on_train_state(state: ProcessState) -> None:
        asyncio.run_coroutine_threadsafe(
            broadcast({"type": M.TRAIN_STATE, "data": state.value}), loop
        )

    def on_train_metrics(metrics: dict) -> None:
        asyncio.run_coroutine_threadsafe(
            broadcast({"type": M.TRAIN_METRICS, "data": metrics}), loop
        )

    train_manager.set_log_callback(on_train_log)
    train_manager.set_state_callback(on_train_state)
    train_manager.set_metrics_callback(on_train_metrics)

    # ── 녹화 콜백 ──
    def on_record_log(line: str) -> None:
        asyncio.run_coroutine_threadsafe(
            broadcast({"type": M.RECORD_LOG, "data": line}), loop
        )

    def on_record_state(state: ProcessState) -> None:
        estop_bridge.sync_activities()
        asyncio.run_coroutine_threadsafe(
            broadcast({"type": M.RECORD_STATE, "data": state.value}), loop
        )

    def on_record_status(status: dict) -> None:
        asyncio.run_coroutine_threadsafe(
            broadcast({"type": M.RECORD_STATUS, "data": status}), loop
        )

    record_manager.set_log_callback(on_record_log)
    record_manager.set_state_callback(on_record_state)
    record_manager.set_status_callback(on_record_status)

    # ── 정책 서버 콜백 ──
    def on_ps_log(line: str) -> None:
        asyncio.run_coroutine_threadsafe(
            broadcast({"type": M.PS_LOG, "data": line}), loop
        )

    def on_ps_state(state: ProcessState) -> None:
        asyncio.run_coroutine_threadsafe(
            broadcast({"type": M.PS_STATE, "data": state.value}), loop
        )

    policy_server_manager.pm.set_log_callback(on_ps_log)
    policy_server_manager.pm.set_state_callback(on_ps_state)

    # ── 데이터셋 업로드 콜백 ──
    from app.routers.datasets import _upload_pm

    def on_upload_log(line: str) -> None:
        asyncio.run_coroutine_threadsafe(
            broadcast({"type": M.UPLOAD_LOG, "data": line}), loop
        )

    def on_upload_state(state: ProcessState) -> None:
        asyncio.run_coroutine_threadsafe(
            broadcast({"type": M.UPLOAD_STATE, "data": state.value}), loop
        )

    _upload_pm.set_log_callback(on_upload_log)
    _upload_pm.set_state_callback(on_upload_state)


@router.websocket("/ws")
async def websocket_endpoint(ws: WebSocket) -> None:
    _setup_callbacks()
    process_manager.ensure_log_reader()

    await ws.accept()
    _clients.add(ws)
    await ws.send_json({"type": M.STATE, "data": process_manager.state.value})
    await ws.send_json({"type": M.TRAIN_STATE, "data": train_manager.state.value})
    await ws.send_json({"type": M.RECORD_STATE, "data": record_manager.state.value})

    try:
        while True:
            data = await ws.receive_json()
            if data.get("type") == "ping":
                await ws.send_json({"type": M.PONG})
    except WebSocketDisconnect:
        pass
    finally:
        _clients.discard(ws)
