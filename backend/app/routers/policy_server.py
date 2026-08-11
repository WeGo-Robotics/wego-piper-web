"""정책 서버 관리 API."""

import logging

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.services.exclusivity import Activity, require_idle
from app.services.policy_server_manager import policy_server_manager

router = APIRouter(prefix="/api/policy-server", tags=["policy-server"])
logger = logging.getLogger(__name__)


class CheckRemoteRequest(BaseModel):
    address: str  # "192.168.1.100:8088"


@router.post("/check-remote")
async def check_remote_server(body: CheckRemoteRequest):
    """원격 gRPC 정책 서버 연결 확인."""
    try:
        import grpc
        channel = grpc.insecure_channel(body.address)
        try:
            grpc.channel_ready_future(channel).result(timeout=3)
            return {"reachable": True, "address": body.address}
        except grpc.FutureTimeoutError:
            return {"reachable": False, "address": body.address, "error": "연결 시간 초과 (3초)"}
        finally:
            channel.close()
    except Exception as e:
        return {"reachable": False, "address": body.address, "error": str(e)}


class PolicyServerStartRequest(BaseModel):
    host: str = "127.0.0.1"
    port: int = 8088
    fps: int = 30


@router.post("/start")
async def start_policy_server(body: PolicyServerStartRequest):
    """정책 서버 시작."""
    require_idle(Activity.POLICY_SERVER)
    try:
        await policy_server_manager.start(host=body.host, port=body.port, fps=body.fps)
    except Exception as e:
        raise HTTPException(500, f"정책 서버 시작 실패: {e}")
    return {"status": "started", "pid": policy_server_manager.pm.pid, "address": policy_server_manager.address}


@router.post("/stop")
async def stop_policy_server():
    """정책 서버 정지."""
    await policy_server_manager.stop()
    return {"status": "stopped"}


@router.get("/status")
async def policy_server_status():
    """정책 서버 상태."""
    return policy_server_manager.get_status()
