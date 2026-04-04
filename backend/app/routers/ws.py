import asyncio
import json

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app.services.process_manager import ProcessState, process_manager

router = APIRouter()

_clients: set[WebSocket] = set()


async def broadcast(message: dict) -> None:
    for client in list(_clients):
        try:
            await client.send_json(message)
        except Exception:
            _clients.discard(client)


def _setup_callbacks() -> None:
    loop = asyncio.get_running_loop()

    def on_log(line: str) -> None:
        if line.startswith("{"):
            try:
                data = json.loads(line)
                if data.get("t") == "telemetry":
                    asyncio.run_coroutine_threadsafe(
                        broadcast({"type": "telemetry", "data": data}), loop
                    )
                    return
            except json.JSONDecodeError:
                pass
        asyncio.run_coroutine_threadsafe(
            broadcast({"type": "log", "data": line}), loop
        )

    def on_state_change(state: ProcessState) -> None:
        asyncio.run_coroutine_threadsafe(
            broadcast({"type": "state", "data": state.value}), loop
        )

    process_manager.set_log_callback(on_log)
    process_manager.set_state_callback(on_state_change)


@router.websocket("/ws")
async def websocket_endpoint(ws: WebSocket) -> None:
    # 매 연결마다 콜백을 현재 이벤트 루프에 재등록
    _setup_callbacks()
    # --reload 후 stdout reader가 죽었으면 재시작
    process_manager.ensure_log_reader()

    await ws.accept()
    _clients.add(ws)
    await ws.send_json({"type": "state", "data": process_manager.state.value})

    try:
        while True:
            data = await ws.receive_json()
            if data.get("type") == "ping":
                await ws.send_json({"type": "pong"})
    except WebSocketDisconnect:
        pass
    finally:
        _clients.discard(ws)
