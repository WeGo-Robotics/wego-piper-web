import asyncio
import logging

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.services import hub_client

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/hub", tags=["hub"])


def _account(info: dict) -> dict:
    """`whoami` 응답 → 화면이 쓰는 모양. **로그인 응답도 같은 걸 쓴다.**

    갈라 두면 로그인 직후 화면과 새로고침한 화면이 다른 것을 보여준다.
    """
    token = info.get("auth", {}).get("accessToken", {})
    return {
        "logged_in": True,
        "username": info.get("name", ""),
        "fullname": info.get("fullname", ""),
        "avatar_url": info.get("avatarUrl", ""),
        "orgs": [o.get("name", "") for o in info.get("orgs", [])],
        "token_name": token.get("displayName", ""),
        # "read" 토큰도 whoami 는 성공한다 — 그러면 로그인돼 보이는데
        # **학습 끝 업로드에서만** 실패한다. 그래서 권한을 같이 내린다.
        # (실측 2026-09-02: role 은 "write"/"read". fine-grained 는 다를 수
        #  있어 "read" 일 때만 경고한다.)
        "token_role": token.get("role", ""),
        # ⚠ 자리를 알려 준다. 컨테이너는 `/data/hf/token`, 저장소에서 띄우면
        #   `~/.cache/huggingface/token` 이다 — 호스트에서 `huggingface-cli login`
        #   을 해도 컨테이너는 그 파일을 못 본다.
        "token_path": str(hub_client.token_path()),
    }


@router.get("/whoami")
async def whoami():
    """HuggingFace 로그인 정보 조회."""
    import asyncio

    try:
        # HfApi 를 새로 만들지 않는다 — endpoint 설정이 한쪽에만 걸리면
        # 여기만 조용히 huggingface.co 를 보게 된다.
        # ⚠ whoami 는 **네트워크를 탄다.** 사내망에서 HF 가 느리면 그 시간만큼
        #   이벤트 루프가 서고, 대시보드가 이걸 부르기 시작하면서 노출이 늘었다.
        info = await asyncio.to_thread(hub_client.get_api().whoami)
        return _account(info)
    except Exception as e:
        logger.debug("HF whoami failed: %s", e)
        return {"logged_in": False, "username": "", "fullname": "",
                "token_path": str(hub_client.token_path()), "error": str(e)}


class LoginRequest(BaseModel):
    token: str


@router.post("/login")
async def hub_login(body: LoginRequest):
    """토큰을 검증하고 저장한다.

    ⚠ **토큰을 되돌려 보내지 않는다.** 한 번 넣은 값을 화면에서 다시 읽을 수 있게
    하면 그 화면이 자격증명 유출 경로가 된다. 계정 이름·권한만 돌려준다.
    """
    token = body.token.strip()
    if not token:
        raise HTTPException(400, "토큰이 비었습니다")
    try:
        info = await asyncio.to_thread(hub_client.save_token, token)
    except Exception as e:
        # ⚠ 예외 문자열에 토큰이 섞여 나올 수 있다 — 가리고 내보낸다
        raise HTTPException(401, f"토큰이 거부됐습니다: {str(e).replace(token, '***')[:200]}")
    return _account(info)


@router.post("/logout")
async def hub_logout():
    """토큰 파일을 지운다. 없어도 성공이다 — 결과가 같으니 에러로 만들 이유가 없다."""
    removed = await asyncio.to_thread(hub_client.clear_token)
    return {"logged_in": False, "removed": removed,
            "token_path": str(hub_client.token_path())}


@router.get("/models")
async def list_hub_models(q: str = "", author: str = "lerobot", limit: int = 30):
    return await hub_client.search_models(q, author, limit)


@router.get("/models/{repo_id:path}")
async def hub_model_detail(repo_id: str):
    return await hub_client.get_model_info(repo_id)


@router.get("/datasets")
async def list_hub_datasets(q: str = "", author: str = "lerobot", limit: int = 30):
    return await hub_client.search_datasets(q, author, limit)


@router.get("/datasets/{repo_id:path}")
async def hub_dataset_detail(repo_id: str):
    """Hub 의 데이터셋 정보. **없으면 404 다.**

    ⚠ 예전에는 `HfHubHTTPError` 를 안 잡아 "없음" 이 **500** 으로 나갔다. 그러면
    호출부가 "아직 안 올라감" 과 "Hub 장애" 를 구별할 수 없다 — 원격 학습은 시작
    전에 그 둘을 갈라야 한다(§5: 업로드 검증 전에는 provision 하지 않는다).
    """
    try:
        return await hub_client.get_dataset_info(repo_id)
    except Exception as exc:                                        # noqa: BLE001
        status = getattr(getattr(exc, "response", None), "status_code", None)
        if status == 404:
            raise HTTPException(404, f"Hub 에 없는 데이터셋입니다: {repo_id}") from exc
        if status in (401, 403):
            raise HTTPException(
                status, f"접근 권한이 없습니다(로그인 상태를 확인하세요): {repo_id}") from exc
        raise HTTPException(502, f"Hub 조회에 실패했습니다: {str(exc)[:200]}") from exc


class DownloadRequest(BaseModel):
    repo_id: str
    repo_type: str = "model"  # "model" or "dataset"


@router.post("/download")
async def start_download(body: DownloadRequest):
    """받기 시작하고 **지금 상태를 바로 돌려준다.**

    ⚠ 예전에는 `{"status":"started"}` 라는 고정 문구만 돌려줬고, 화면은 그걸 받고 다시는
    묻지 않아 "다운로드 중..." 이 영원히 남았다. 시작 응답과 폴링 응답이 **같은 모양**
    이어야 화면이 한 규칙으로 읽는다.
    """
    return await hub_client.start_download(body.repo_id, body.repo_type)


@router.get("/download/status")
async def download_status(repo_id: str):
    return hub_client.get_download_status(repo_id)


@router.get("/verify")
async def verify_repo(repo_id: str, repo_type: str = "dataset"):
    """이미 받아 둔 것에 **빠진 파일이 있나.** 받는 것과 무관하게 언제든 부를 수 있다.

    ⚠ 받다 만 저장소는 목록·에피소드 수가 멀쩡해 보인다 — 그 숫자는 `meta/` 에서 읽기
    때문이다. 파일이 다 있는지는 세어 봐야 안다(2026-09-23 사고).
    """
    from app.services import hub_download

    missing = await asyncio.to_thread(hub_download.verify, repo_id, repo_type)
    return {"repo_id": repo_id, "repo_type": repo_type,
            "complete": not missing, "missing": missing}
