"""임대 GPU(Vast.ai) — 게이트웨이 키·준비도·오퍼 조회 (feature/vast-training.md §9-1·§9-2).

두 화면이 읽는다:

- **설정 → 클라우드** — 게이트웨이 SSH 키 (`/ssh-key*`). "빌리기 전까지".
- **LeRobot → 클라우드 GPU 의 RENT 탭** — 오퍼·템플릿·준비도. "무엇을 빌릴지 고른다".
- **같은 페이지의 인스턴스 탭** — 지금 도는 것·고아·파기 (`/instances`).

⚠ **끄는 쪽이 먼저 왔다.** `DELETE /instances/{id}` 가 `POST /rent` 보다 먼저 생긴 것은
순서가 뒤바뀐 게 아니다 — 띄울 수만 있고 끌 수 없으면 그게 돈이 새는 구조다(§6).

이제 `POST /rent` 로 빌릴 수 있다. 켤 수 있게 된 것은 버튼을 고쳐서가 아니라 **상한이
셋 다 생겼기 때문**이다: 학습 스크립트의 `timeout`(가장 안쪽) · 예산/시간 틱(바깥) ·
`finally` 로 보장된 파기. 그전까지 [빌리기]가 비활성이던 것은 디자인이 아니라 사실이었다.
"""

import asyncio
from functools import partial
import json
import logging
import shutil
import subprocess
import time
from pathlib import Path
from dataclasses import asdict

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from app.core.config import settings
from app.services.cloud import sshkey
from app.services.cloud.providers import vast
from app.services.cloud.providers.base import MIN_CUDA, OfferFilter
from app.routers.training import TrainStartRequest, train_cli_params
from app.services.cloud import apikey, sweeper
from app.services.cloud.lifecycle import is_orphan

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/cloud", tags=["cloud"])

#: 우리가 만든 인스턴스의 라벨 접두사. ⚠ **레지스트리를 잃어도 남는 유일한
#: 단서**라 고아 판정이 이것에 달려 있다 — 바꾸면 옛 인스턴스를 못 알아본다.
ORPHAN_PREFIX = "piper-"

#: CLI 가 없을 때의 문구. ⚠ **사용자 탓이 아니다** — 배포판은 컨테이너라 사람이
#: 깔 방법이 없다. 고쳐야 할 것은 이미지이지 사용자가 아니라고 말해야 한다.
NO_CLI = ("이 게이트웨이에 vastai CLI 가 없습니다 — 컨테이너 배포판은 이미지에 들어 있어야 "
          "합니다(이미지 갱신 필요). 저장소에서 직접 띄웠다면 `pip install vastai`.")


@router.get("/ssh-key")
async def get_ssh_key():
    """게이트웨이 공개키와 지문. ⚠ **비밀키는 안 나간다.** 없으면 없다고만 한다."""
    return sshkey.info()


@router.post("/ssh-key")
async def create_ssh_key():
    """없으면 만든다(멱등). 있으면 그대로 둔다 — 갈아엎으면 등록해 둔 것이 무효가 된다."""
    return await asyncio.to_thread(sshkey.ensure)


@router.post("/ssh-key/register")
async def register_ssh_key():
    """공개키를 Vast 계정에 등록하고 **지문으로 확인**한다.

    ⚠ 확인이 요점이다. `create ssh-key` 가 성공을 보고해도 계정에 우리 키가 있는지는
    별개이고, 없으면 증상은 인스턴스를 빌린 뒤 접속 실패로 늦게 나타난다.
    """
    if not shutil.which("vastai"):
        raise HTTPException(409, NO_CLI)
    state = await asyncio.to_thread(sshkey.ensure)
    pub, want = state["public_key"], state["fingerprint"]

    def _run(args: list[str]) -> subprocess.CompletedProcess:
        return subprocess.run(["vastai", *args], capture_output=True, text=True, timeout=60)

    out = await asyncio.to_thread(_run, ["create", "ssh-key", pub])
    if out.returncode != 0:
        raise HTTPException(502, f"등록에 실패했습니다: {(out.stderr or out.stdout).strip()[:300]}")

    listed = await asyncio.to_thread(_run, ["show", "ssh-keys", "--raw"])
    found = False
    if listed.returncode == 0:
        try:
            for row in json.loads(listed.stdout) or []:
                k = (row.get("public_key") or row.get("ssh_key") or "").strip()
                if k and sshkey.fingerprint(k) == want:
                    found = True
                    break
        except Exception as exc:                                    # noqa: BLE001
            logger.warning("등록 확인 실패(파싱): %s", exc)
    return {"registered": found, "fingerprint": want,
            "detail": None if found else "등록은 보고됐지만 계정 목록에서 같은 지문을 못 찾았습니다."}


# ─────────────────────────────────────────────────────────────────────────────
# RENT 탭 — 오퍼·템플릿·준비도 (§9-2)
# ─────────────────────────────────────────────────────────────────────────────

#: 프로바이더는 상태가 없다(캐시는 모듈 안). 저장된 키가 있으면 **여기서 넘긴다** —
#: 없으면 빈 문자열이라 CLI 가 제 파일을 본다(개발 머신은 그 경로로 돈다).
def _provider() -> vast.VastProvider:
    return vast.VastProvider(apikey.load() or None)


class ApiKeyRequest(BaseModel):
    api_key: str


@router.get("/credentials")
async def get_credentials():
    """키가 설정돼 있나. **키 자체는 안 준다** — 끝 네 자리까지다.

    ⚠ 게이트웨이는 `/api/ext/v1` 말고는 인증이 없다. LAN 에서 이 포트에 닿는 누구나
    이 응답을 읽으므로, 전체를 보여 주는 순간 그 화면이 곧 유출 경로가 된다.
    """
    return apikey.status()


@router.put("/credentials")
async def put_credentials(body: ApiKeyRequest):
    """키를 저장한다. **먼저 써 보고, 되면 저장한다.**

    ⚠ 검증 없이 저장하면 "설정됨" 이라 표시되는데 아무것도 안 되는 상태가 된다 —
    제일 헷갈리는 실패다. 그래서 그 키로 계정을 조회해 보고, 통과할 때만 파일에 쓴다.

    ⚠ 키는 **argv 가 아니라 env 로** 넘어간다(`VastProvider`). `ps` 로 남의 프로세스
    인자를 읽을 수 있는 기계에서 argv 는 비밀을 두는 자리가 아니다.
    """
    key = (body.api_key or "").strip()
    if not key:
        raise HTTPException(400, "키가 비었습니다")
    try:
        account = await asyncio.to_thread(vast.VastProvider(key).whoami)
    except FileNotFoundError as exc:
        raise HTTPException(409, NO_CLI) from exc
    except Exception as exc:                                        # noqa: BLE001
        # ⚠ **틀린 키는 400 이다.** `_to_http` 는 이걸 502 로 올리는데, 그건 "게이트웨이
        #   쪽이 고장" 이라는 뜻이라 사람이 엉뚱한 곳을 본다 — 실제로는 붙여넣기를 다시
        #   해야 하는 상황이다. Vast 의 원문은 로그에만 남긴다(키가 섞일 수 있다).
        logger.warning("Vast 키 검증 실패: %s", str(exc)[:200])
        raise HTTPException(
            400, "그 키로는 계정을 읽지 못했습니다 — 키를 다시 확인해 주세요 "
                 "(vast.ai → Account → API Keys)") from exc
    if not account:
        raise HTTPException(400, "그 키로는 계정을 읽지 못했습니다 — 다시 확인해 주세요")
    await asyncio.to_thread(apikey.save, key)
    logger.info("Vast API 키를 저장했습니다 (끝 %s)", apikey.tail(key))
    # ⚠ 응답에도 키는 없다. 사람이 "맞게 들어갔나" 를 확인할 근거는 크레딧과 끝 네 자리다.
    return {**apikey.status(), "credit": account.get("credit")}


@router.delete("/credentials")
async def delete_credentials():
    """저장한 키를 지운다.

    ⚠ `.env` 로 심은 키는 못 지운다 — 그건 운영자가 배포 수단으로 넣은 값이고, 화면이
    그걸 지우면 운영자는 자기 키가 왜 사라졌는지 알 길이 없다. 대신 그 사실을 말한다.
    """
    removed = await asyncio.to_thread(apikey.clear)
    st = apikey.status()
    if st["source"] == "env":
        return {**st, "removed": removed,
                "detail": "환경변수(VAST_API_KEY)의 키가 남아 있습니다 — 그건 배포 설정이라 "
                          "여기서 못 지웁니다"}
    return {**st, "removed": removed}


def _to_http(exc: Exception) -> HTTPException:
    """예외를 사람이 읽을 응답으로. **CLI 없음은 고장이 아니다**(§9-1 과 같은 규칙)."""
    if isinstance(exc, FileNotFoundError):
        return HTTPException(409, NO_CLI)
    if isinstance(exc, ValueError):
        return HTTPException(400, str(exc))
    if isinstance(exc, subprocess.TimeoutExpired):
        return HTTPException(504, "Vast 응답이 없습니다 — 잠시 뒤 새로고침해 주세요")
    return HTTPException(502, f"Vast 조회에 실패했습니다: {str(exc)[:300]}")


@router.get("/vast/offers")
async def vast_offers(
    gpu: list[str] = Query(["RTX 4090"]),
    num_gpus: int = Query(1, ge=1, le=8),
    min_cuda: float = Query(MIN_CUDA, ge=0, le=99),
    min_reliability: float = Query(0.98, ge=0, le=1),
    min_inet: float = Query(200.0, ge=0, le=100_000),
    min_cpu: float | None = Query(None, ge=0, le=512),
    max_price: float | None = Query(None, gt=0, le=1000),
    disk_gb: float = Query(40.0, ge=5, le=2000),
    limit: int = Query(50, ge=1, le=200),
    cuda: str = Query("", max_length=8, pattern=r"^(cu\d{3})?$"),
    refresh: bool = False,
):
    """빌릴 수 있는 기계 목록.

    ⚠ **`dph_total` 이 아니라 `hourly` 를 화면에 써야 한다.** `dph_total` 에 섞인
    디스크는 검색 기본값(~5GB)이고 `hourly` 는 요청한 `disk_gb` 로 다시 계산한 값이다.
    호스트마다 `storage_cost` 가 4배까지 벌어지므로 일률적인 보정으로는 못 맞춘다.

    ⚠ 준비도가 빨간불이어도 **이 목록은 막지 않는다.** 세팅하기 전에 가격부터 보고
    싶은 게 사람이다 — 막는 것은 [빌리기] 버튼 하나뿐이다(§9-2).

    `gpu` 는 **여러 번 줄 수 있고**(`?gpu=RTX+4090&gpu=RTX+3060`), **빈 값이면 전체**다
    (`?gpu=`). 사용자가 "싹 다 보여줘" 라고 했다 — 한 기종만 고르게 하는 계약이면
    그 말을 못 들어준다.

    ⚠ **`cuda` 는 고른 템플릿의 CUDA 빌드다**(`cu126`·`cu128`). 호환 판정이 이걸 따라
    간다 — 두 빌드는 담고 있는 커널이 다르고 **한쪽이 다른 쪽의 상위집합이 아니다**
    (cu128 은 Blackwell 을 얻는 대신 맥스웰·파스칼·V100 을 잃는다). 안 주면 기본값
    기준으로 답한다.
    """
    f = OfferFilter(
        gpu_names=tuple(gpu), num_gpus=num_gpus, min_cuda=min_cuda,
        min_reliability=min_reliability, min_inet_down=min_inet,
        min_cpu_cores=min_cpu, max_price=max_price, disk_gb=disk_gb, limit=limit,
    )
    try:
        offers = await asyncio.to_thread(
            partial(_provider().search, f, refresh=refresh, cuda_build=cuda))
    except Exception as exc:                                        # noqa: BLE001
        raise _to_http(exc) from exc
    return {"query": vast.build_query(f), "disk_gb": disk_gb,
            "offers": [asdict(o) for o in offers]}


@router.get("/gpus")
async def cloud_gpus(
    num_gpus: int = Query(1, ge=1, le=8),
    min_cuda: float = Query(MIN_CUDA, ge=0, le=99),
    min_reliability: float = Query(0.98, ge=0, le=1),
    min_inet: float = Query(200.0, ge=0, le=100_000),
    min_cpu: float | None = Query(None, ge=0, le=512),
    max_price: float | None = Query(None, gt=0, le=1000),
    disk_gb: float = Query(40.0, ge=5, le=2000),
    # ⚠ 고른 템플릿의 CUDA 빌드. 선택지의 `support` 판정이 이걸 따라간다 — 표와
    #   선택지가 **같은 기준**을 봐야 "고를 수 있는데 표는 0행" 이 안 생긴다.
    cuda: str = Query("", max_length=8, pattern=r"^(cu\d{3})?$"),
    refresh: bool = False,
):
    """고를 수 있는 **GPU 기종** — 선택지를 채우는 목록.

    ⚠ **표와 같은 필터를 받는다.** 카탈로그가 자기 조건으로 만들어지면 "선택지에는
    있는데 표는 0행" 이 된다. 그래서 `gpu` 만 빼고 같은 파라미터를 받는다.

    ⚠ **고정 목록을 두지 않는다.** 예전에 화면에 7개를 박아 뒀는데 사용자가 쓰려는
    3060 이 없었고, 대신 RTX 5090(sm_120 — 우리 cu126 이미지로는 못 돈다)이 들어
    있었다. 박아 둔 목록은 이렇게 조용히 틀린다.

    실패하면 빈 목록을 준다 — 화면이 지금 오퍼에 보이는 기종과 합쳐 쓰므로 선택
    자체가 막히지는 않는다.
    """
    f = OfferFilter(
        gpu_names=(), num_gpus=num_gpus, min_cuda=min_cuda,
        min_reliability=min_reliability, min_inet_down=min_inet,
        min_cpu_cores=min_cpu, max_price=max_price, disk_gb=disk_gb,
    )
    try:
        rows = await asyncio.to_thread(
            partial(_provider().catalog, f, refresh=refresh, cuda_build=cuda))
    except Exception as exc:                                        # noqa: BLE001
        logger.warning("GPU 카탈로그 조회 실패: %s", exc)
        return {"gpus": [], "disk_gb": disk_gb,
                "detail": f"기종 목록을 불러오지 못했습니다: {str(exc)[:200]}"}
    return {"gpus": [asdict(g) for g in rows], "disk_gb": disk_gb, "detail": None}


class RentRequest(TrainStartRequest):
    """[빌리기] 한 번. **상한이 요청의 일부다** — 기본값으로 빠져나갈 수 없게.

    ⚠ **학습 쪽 필드는 `TrainStartRequest` 에서 물려받는다.** 따로 적어 두던 시절에는
    학습 페이지가 보내는 22개 중 **9개만** 받았고 나머지는 Pydantic 이 조용히 버렸다 —
    거기 `pretrained_path` 가 있었다. 즉 파인튜닝을 걸어도 **말없이 처음부터** 학습이
    됐고, 그걸 아는 시점은 몇 시간 뒤 결과를 열어 봤을 때다.
    """

    # ⚠ 기본값이 있는 이유는 **미리보기** 때문이다. `/rent/preview` 는 무엇이 돌지만
    #   보여 주므로 기계가 필요 없다 — 실제로 빌릴 때는 아래 `/rent` 가 막는다.
    offer_id: int = 0
    template_hash: str = ""
    disk_gb: float = Field(40.0, ge=10, le=2000)
    budget_usd: float = Field(10.0, gt=0, le=1000)
    max_hours: float = Field(6.0, gt=0, le=72)

    # ⚠ 임대는 **시간당 과금**이라 기본값을 낮춰 잡는다. 학습 페이지 기본값(10만 스텝)을
    #   그대로 물려받으면, 값을 안 보낸 호출 하나가 큰 청구서가 된다.
    steps: int = Field(5000, ge=1)
    log_freq: int = 100
    save_freq: int = 1000

    # ⚠ **회수 경로라 필수다.** 없으면 `cli_mapping` 이 `--policy.push_to_hub=false` 를
    #   강제하고, 학습은 멀쩡히 끝나고 가중치만 사라진다(§12-4: 푸시는 한 번뿐).
    policy_repo_id: str

    #: 중간 체크포인트도 받을까. 기본은 **안 받는다** — 최종본만 오는 것이 싸다.
    #:
    #: ⚠ Hub 로는 애초에 못 받는다. `push_to_hub` 는 학습이 끝날 때 **한 번만** 올려서
    #: (§12-4), `save_freq` 를 아무리 잘게 줘도 Hub 에는 최종본뿐이다. 중간 것은 기계
    #: 안에만 있고 파기하면 같이 사라진다 — 20K 학습에 5000마다 저장했는데 하나만
    #: 돌아오는 것이 그 때문이다.
    #:
    #: ⚠ 켜면 기계가 살아 있는 동안 `scp` 로 끌어온다. 실측 기준 한 벌 200MB · 나가는
    #: 트래픽 $0.017/GB 라 네 벌이면 약 $0.014 다.
    fetch_checkpoints: bool = False


class NewInstanceRequest(BaseModel):
    """기계 **하나만** 만든다 — 학습은 안 건다.

    ⚠ 학습 설정이 여기 없는 것이 요점이다. 클라우드 페이지는 "기계", 학습 페이지는
    "학습" 으로 가른다 — 학습 폼이 두 곳에 있으면 반드시 어긋난다(§12-17 이 그 병이었다).
    """

    offer_id: int
    template_hash: str
    disk_gb: float = Field(40.0, ge=10, le=2000)


@router.post("/instances")
async def create_instance(body: NewInstanceRequest):
    """기계를 만든다. **끄는 것은 사람이 한다.**

    ⚠ 라벨이 `piper-box-` 다. 이게 "사람이 일부러 빌린 것" 이라는 표시이고, 고아
    스캐너가 이걸 보고 **고아 신고를 안 한다** — 관리하는 태스크가 없는 것이 이쪽의
    정상이기 때문이다. 대신 학습 없이 오래 떠 있으면 유휴로 말해 준다.

    ⚠ 라벨에 기록하는 이유: 레지스트리에 적으면 게이트웨이가 죽는 순간 잃는다(§12-14).
    이건 **사람의 결정**이라 프로세스보다 오래 살아야 한다.

    ⚠ **자동 상한이 없다.** 한 묶음(`/rent`)과 달리 예산·시간 가드가 붙지 않는다 —
    빈 기계도 요금은 똑같이 나가므로 끄는 것을 잊으면 그대로 청구된다.
    """
    label = f"{sweeper.BOX_PREFIX}{int(time.time()) % 100000}"
    try:
        inst = await asyncio.to_thread(
            partial(_provider().create, body.offer_id,
                    template_hash=body.template_hash, disk_gb=body.disk_gb, label=label))
    except Exception as exc:                                        # noqa: BLE001
        raise _to_http(exc) from exc
    logger.warning("기계를 빌렸습니다: %s (%s) — **끄는 것은 사람이 합니다**",
                   inst.id, label)
    return {"created": True, **asdict(inst)}


def _rent_train_args(body: "RentRequest") -> list[str]:
    """[빌리기]가 **실제로 돌릴** 학습 인자.

    ⚠ 미리보기(`/rent/preview`)와 **같은 함수**를 쓴다. 두 벌로 두면 반드시 갈리고,
    그러면 화면이 보여 준 명령과 도는 명령이 달라진다.

    ⚠ 그리고 인자 조립 자체는 `/training/start` 와도 **같은 함수**(`train_cli_params`)를
    쓴다 — 학습 페이지에 필드가 하나 늘었을 때 임대 경로에서만 조용히 빠지는 일을
    구조로 막는다.

    ⚠ **여기서는 파일시스템을 안 만진다.** `/training/start` 는 `pretrained_path` 로
    `resolve_rename_map()`·`apply_dim_overrides()` 를 부르는데, 그건 **이 기계의**
    체크포인트를 읽고 고치는 일이다. 빌린 기계에는 그 경로가 없다.
    """
    from app.core.cli_mapping import build_train_args

    params, _amp, _title, _desc = train_cli_params(body)
    return build_train_args(params, python=settings.train_remote_python)


def _reject_local_only_settings(body: "RentRequest") -> None:
    """빌린 기계에서 **말이 안 되는 설정**을 미리 막는다.

    ⚠ `pretrained_path` 가 이 기계의 경로면 임대 서버에는 그 파일이 없다. 그대로 걸면
    학습이 몇 분 뒤 `No such file or directory` 로 죽거나, 더 나쁘게는 lerobot 이
    그걸 Hub 저장소 이름으로 읽어 엉뚱한 것을 받는다. Hub 저장소 이름(`org/name`)이면
    원격에서도 멀쩡하므로 그건 막지 않는다.
    """
    path = (body.pretrained_path or "").strip()
    if not path:
        return
    looks_local = path.startswith(("/", "./", "~")) or Path(path).exists()
    if looks_local:
        raise HTTPException(
            400, f"이어서 학습할 체크포인트가 이 기계의 경로입니다({path}) — 빌린 "
                 f"기계에는 그 파일이 없습니다. Hub 저장소 이름(org/name)으로 주거나, "
                 f"먼저 저장소에 올린 뒤 다시 거세요.")


@router.post("/rent/preview")
async def rent_preview(body: RentRequest):
    """빌리기 전에 **무엇이 돌지** 보여 준다. 부작용이 없다.

    ⚠ **환경변수는 안 돌려준다.** `_train_env()` 에는 HF 토큰이 들어 있다 — 미리보기
    한 번에 화면으로 키가 새면 그게 더 큰 사고다. AMP 는 화면이 이미 알고 있다.
    """
    args = _rent_train_args(body)
    return {"args": args, "command": " ".join(args)}


@router.post("/rent")
async def rent_and_train(body: RentRequest):
    """빌려서 학습을 걸고, **끝나면 반드시 파기한다**.

    ⚠ **`policy_repo_id` 가 없으면 받지 않는다.** 없으면 `cli_mapping` 이
    `--policy.push_to_hub=false` 를 강제하고, 학습은 멀쩡히 끝나고 **가중치만 사라진다.**
    그리고 실측(§12-4)으로 푸시는 종료 시점 한 번뿐이라 그 한 번이 유일한 기회다.

    ⚠ **동시에 하나만.** 두 번째를 받아 주면 첫 인스턴스의 핸들을 잃는다 = 고아다.
    """
    from app.routers.training import _require_push_permission, _train_env
    from app.services.cloud import rent
    from app.services.cloud.lifecycle import Budget
    from app.services.exclusivity import Activity, require_idle

    require_idle(Activity.TRAINING)
    if rent.busy():
        raise HTTPException(409, "이미 임대 학습이 돌고 있습니다 — 하나씩만 돌립니다")
    if not body.policy_repo_id.strip():
        raise HTTPException(
            400, "가중치를 올릴 저장소(policy_repo_id)가 필요합니다 — 없으면 학습이 "
                 "끝나도 결과를 가져올 수 없습니다.")
    _reject_local_only_settings(body)
    await _require_push_permission(body.policy_repo_id)

    args = _rent_train_args(body)

    try:
        job = await rent.start(
            provider=_provider(), offer_id=body.offer_id,
            template_hash=body.template_hash, disk_gb=body.disk_gb,
            budget=Budget(usd=body.budget_usd, max_hours=body.max_hours),
            args=args, total_steps=body.steps,
            env=_train_env(body.amp, remote=True) or {},
            # ⚠ 회수 보험이 "Hub 에 갔나" 를 물어볼 대상이다 (§12-4).
            repo_id=body.policy_repo_id,
            fetch_checkpoints=body.fetch_checkpoints)
    except RuntimeError as exc:
        raise HTTPException(409, str(exc)) from exc
    return {"started": True, **job.to_dict()}


class TrainOnRequest(RentRequest):
    """**이미 있는 기계**에 학습을 건다 — 빌리지도, 끄지도 않는다.

    ⚠ `RentRequest` 를 물려받는 이유는 학습 필드를 하나도 안 흘리기 위해서다(§12-18).
    다만 기계를 고르는 방식이 다르다 — 오퍼가 아니라 **인스턴스 번호**다.
    """

    instance_id: int
    # 빌리는 요청이 아니므로 오퍼·템플릿은 필요 없다
    offer_id: int = 0
    template_hash: str = ""
    # ⚠ 예산 상한은 여기 없다. 기계가 우리 것이 아니라 끌 수 없고, 못 끄는 상한은
    #   상한이 아니라 장식이다. 남는 것은 **학습 자체의 시간 상한**뿐이다(§6-1).
    budget_usd: float = 0.0


@router.post("/train-on")
async def train_on_instance(body: TrainOnRequest):
    """빌려 둔 기계에 학습을 얹는다. **끝나도 기계는 그대로 둔다.**

    ⚠ 파기가 없는 대신 두 가지는 그대로다 — 학습 스크립트의 `timeout`(게이트웨이가
    죽어도 학습은 끝난다)과 **회수**(가중치는 집으로 온다).

    ⚠ 학습이 끝난 기계는 유휴다. 요금은 똑같이 나가므로 스캐너가 일정 시간 뒤부터
    "몇 분째 학습 없이 떠 있습니다" 를 말한다 — 끄는 것은 인스턴스 탭에서 사람이 한다.
    """
    from app.routers.training import _require_push_permission, _train_env
    from app.services.cloud import rent
    from app.services.exclusivity import Activity, require_idle

    require_idle(Activity.TRAINING)
    if rent.busy():
        raise HTTPException(409, "이미 임대 학습이 돌고 있습니다 — 하나씩만 돌립니다")
    if not body.policy_repo_id.strip():
        raise HTTPException(
            400, "가중치를 올릴 저장소(policy_repo_id)가 필요합니다 — 없으면 학습이 "
                 "끝나도 결과를 가져올 수 없습니다.")
    _reject_local_only_settings(body)
    await _require_push_permission(body.policy_repo_id)

    try:
        job = await rent.train_on(
            provider=_provider(), instance_id=body.instance_id,
            args=_rent_train_args(body), total_steps=body.steps,
            env=_train_env(body.amp, remote=True) or {},
            repo_id=body.policy_repo_id, max_hours=body.max_hours,
            fetch_checkpoints=body.fetch_checkpoints)
    except RuntimeError as exc:
        raise HTTPException(409, str(exc)) from exc
    return {"started": True, **job.to_dict()}


@router.get("/rent")
async def rent_status():
    """지금 도는 임대 학습의 단계와 비용. 없으면 `null`.

    ⚠ **회수를 같이 준다.** 파기가 끝나도 일이 안 끝났을 수 있다 — Hub 에서 가중치를
    받는 중이면 `retrieval.state` 가 `pulling` 이다. 이걸 안 보여 주면 화면은
    "파기됨" 에서 멈춘 채로 있고, 사람은 다 끝난 줄 알고 자리를 뜬다.
    """
    from app.services.cloud import rent, retrieve

    job = rent.current()
    return {"busy": rent.busy(), "job": job.to_dict() if job else None,
            "retrieval": retrieve.status()}


@router.post("/rent/stop")
async def rent_stop():
    """사람이 멈춘다. ⚠ **학습만 세우지 않는다** — 기계까지 파기해야 과금이 멈춘다."""
    from app.services.cloud import rent

    job = await rent.stop_now(_provider())
    if job is None:
        raise HTTPException(404, "도는 임대 학습이 없습니다")
    return job.to_dict()


@router.get("/instances")
async def cloud_instances():
    """지금 살아 있는 인스턴스 — **돈이 나가고 있는 것들**.

    ⚠ **고아를 같이 표시한다.** 우리 라벨(`piper-`)이 붙었는데 레지스트리가 모르는
    것이 고아다 — 게이트웨이가 죽었다 살아났거나 레코드를 잃었을 때 생긴다(§6-3).
    라벨이 레지스트리를 잃고도 남는 유일한 단서다.

    ⚠ **자동으로 파기하지 않는다.** 다른 기계의 게이트웨이가 돌리는 학습일 수 있다
    (§10 결정 5) — 남의 것을 끄는 쪽이 더 나쁜 실수다. 보여 주고 버튼을 주는
    데까지가 우리 몫이다.
    """
    try:
        rows = await asyncio.to_thread(_provider().list_instances)
    except Exception as exc:                                        # noqa: BLE001
        raise _to_http(exc) from exc

    # ⚠ 판정은 **스캐너와 같은 함수**로 한다. 정의가 둘이면 탭과 배너가 다른 말을
    #   한다 — 하나는 고아라 하고 하나는 아니라 하면 사람은 둘 다 안 믿는다.
    known = sweeper.known_instances()
    out = []
    for i in rows:
        d = asdict(i)
        # ⚠ 판정은 **스캐너와 같은 함수**다. 사본을 들고 있다가 실제로 어긋났다 —
        #   사람이 일부러 빌린 기계(`piper-box-`)를 탭만 빨갛게 칠했다.
        d["orphan"] = is_orphan(i, known, ORPHAN_PREFIX, sweeper.BOX_PREFIX)
        out.append(d)
    # 고아를 먼저 — 사람이 봐야 할 것이 위로 온다
    out.sort(key=lambda d: (not d["orphan"], d["id"]))
    return {"instances": out,
            "orphans": sum(1 for d in out if d["orphan"]),
            "known": sorted(known)}


@router.get("/orphans")
async def orphans():
    """마지막 고아 스캔 결과. **여기서 Vast 를 부르지 않는다.**

    ⚠ 배너 하나 때문에 페이지마다 20초짜리 조회를 걸면 안 된다. 실제 조회는 배경
    스캐너가 10분마다 하고(§6-3), 화면은 그 결과를 받아 간다. 지금 이 순간의
    목록이 필요하면 `/instances` 가 직접 조회한다.

    `scanned=false` 는 **"고아가 없다"가 아니라 "아직 안 봤다"** 이다 — 기동
    직후이거나 `vastai` 가 없는 설치다. 화면은 그 둘을 구분해야 한다.
    """
    return sweeper.snapshot()


@router.delete("/instances/{instance_id}")
async def destroy_instance(instance_id: int):
    """인스턴스를 파기한다. **확인될 때만 성공이라고 말한다.**

    ⚠ 실측 함정 둘이 프로바이더 안에 박혀 있다 — `-y` 가 없으면 프롬프트에서 멈춰
    `Aborted.` 를 찍고 **종료코드 0** 으로 끝나고(살아 있는데 성공으로 읽힌다),
    `destroy -y --raw` 는 **빈 출력**이라 응답으로는 판정할 수 없다. 그래서 판정은
    목록으로 한다.

    ⚠ 확인이 안 되면 **200 으로 넘어가지 않는다.** 조용한 성공이 이 경로에서 가장
    비싼 거짓말이다 — 사람이 껐다고 믿고 자리를 뜬다.
    """
    try:
        gone = await asyncio.to_thread(_provider().destroy, int(instance_id))
    except Exception as exc:                                        # noqa: BLE001
        raise _to_http(exc) from exc
    if not gone:
        raise HTTPException(
            502, f"인스턴스 {instance_id} 가 사라진 것을 확인하지 못했습니다 — "
                 f"과금이 계속될 수 있습니다. `vastai show instances` 로 확인하세요.")
    return {"destroyed": True, "instance_id": int(instance_id)}


@router.get("/templates")
async def cloud_templates(refresh: bool = False):
    """우리가 올려 둔 학습 템플릿.

    ⚠ `private=true` 없이 부르면 공개 2048개가 오고 **우리 건 하나도 없다**(§상태).
    프로바이더가 그 질의를 쥐고 있다 — 여기서 다시 쓰지 않는다.
    """
    try:
        rows = await asyncio.to_thread(_provider().templates, refresh=refresh)
    except Exception as exc:                                        # noqa: BLE001
        raise _to_http(exc) from exc
    return {"templates": [asdict(t) for t in rows]}


async def _ssh_registered(want: str | None) -> tuple[bool | None, str]:
    """계정에 **같은 지문**이 있나 — `(판정, 사유)`.

    판정은 셋이다: `True` 등록됨 · `False` 없음 · **`None` 확인 못 했다.**

    ⚠ **세 번째를 두 번째로 뭉개면 안 된다.** 실측(2026-09-18): `vastai show ssh-keys`
    가 401 로 죽어 판정이 `None` 이었는데 화면은 "등록을 눌러 주세요" 라고 말했다. 키는
    이미 등록돼 있었고, 그 버튼을 눌러도 같은 자리로 돌아온다 — **빠져나갈 수 없는
    안내**다. 사유를 같이 돌려주는 이유가 그것이다.
    """
    if not want:
        return None, "게이트웨이 키가 없습니다"
    if not shutil.which("vastai"):
        return None, NO_CLI

    def _go() -> tuple[bool | None, str]:
        out = subprocess.run(["vastai", "show", "ssh-keys", "--raw"],
                             capture_output=True, text=True, timeout=45)
        body = (out.stdout or "").strip() or (out.stderr or "").strip()
        if out.returncode != 0:
            return None, f"목록을 못 읽었습니다: {body[:120]}"
        try:
            rows = json.loads(body)
        except json.JSONDecodeError:
            # ⚠ CLI 는 오류도 종료코드 0 으로 내고 본문에 싣는다 — 그 본문을 그대로
            #   보여 준다. "확인 못 했다" 로만 끝내면 사람이 어디를 볼지 모른다.
            return None, f"목록을 못 읽었습니다: {body[:120]}"
        if not isinstance(rows, list):
            return None, f"목록이 배열이 아닙니다: {str(rows)[:120]}"
        for row in rows:
            k = (row.get("public_key") or row.get("ssh_key") or "").strip()
            if k:
                try:
                    if sshkey.fingerprint(k) == want:
                        return True, ""
                except ValueError:
                    continue
        return False, f"계정의 키 {len(rows)}개 중 같은 지문이 없습니다"

    try:
        return await asyncio.to_thread(_go)
    except Exception as exc:                                        # noqa: BLE001
        logger.warning("SSH 키 등록 확인 실패: %s", exc)
        return None, f"확인 중 오류: {str(exc)[:120]}"


@router.get("/readiness")
async def readiness():
    """빌리기 전에 초록불이어야 하는 것들. **한 항목이 넘어져도 나머지는 답한다.**

    ⚠ 이 응답은 목록을 막는 데 쓰지 않는다. [빌리기] 버튼 하나만 막고, 무엇이
    모자란지 말해 준다 — 그래야 세팅 전에도 가격을 볼 수 있다(§9-2).
    """
    cli = shutil.which("vastai") is not None
    key = sshkey.info()

    account: dict | None = None
    account_error: str | None = None
    templates: list[dict] = []
    if cli:
        try:
            account = await asyncio.to_thread(_provider().whoami)
        except Exception as exc:                                    # noqa: BLE001
            account_error = str(exc)[:200]
        try:
            templates = [asdict(t) for t in await asyncio.to_thread(_provider().templates)]
        except Exception as exc:                                    # noqa: BLE001
            logger.warning("템플릿 조회 실패: %s", exc)

    registered, reg_why = (await _ssh_registered(key.get("fingerprint")) if cli
                           else (None, NO_CLI))

    checks = {
        "cli": {"ok": cli, "detail": None if cli else NO_CLI},
        "api_key": {"ok": account is not None,
                    "detail": account_error or (None if account else "API 키가 설정되지 않았습니다")},
        # ⚠ **"확인 못 했다" 를 "안 돼 있다" 로 말하지 않는다.** 실측(2026-09-18):
        #   `show ssh-keys` 가 401 로 죽어 판정이 `None` 이었는데 화면은 "등록을 눌러
        #   주세요" 라고 했다. 키는 이미 등록돼 있었고, 그 버튼을 눌러도 같은 자리로
        #   돌아온다 — 사람이 빠져나갈 수 없는 안내였다.
        #
        # ⚠ 그래서 **모를 때는 막지 않는다.** 막는 쪽이 더 나쁘다: 제대로 해 둔 사람이
        #   영영 못 빌린다. 대신 무엇을 못 했는지 말하고, 빌린 뒤 접속이 안 되면 여기를
        #   의심하라고 적는다.
        "ssh_key": {"ok": bool(key.get("exists")) and registered is not False,
                    "exists": bool(key.get("exists")),
                    "registered": registered,
                    "fingerprint": key.get("fingerprint"),
                    "detail": (
                        None if registered is True else
                        "설정 → 클라우드 에서 키를 만들어 주세요" if not key.get("exists") else
                        "설정 → 클라우드 에서 [Vast 계정에 등록] 을 눌러 주세요"
                        if registered is False else
                        f"등록 여부를 확인하지 못했습니다 ({reg_why}) — 이미 등록했다면 "
                        f"그대로 두세요. 빌린 뒤 접속이 안 되면 여기를 의심하세요")},
        "template": {"ok": bool(templates),
                     "detail": None if templates else "학습 템플릿을 찾지 못했습니다"},
    }
    # ⚠ 잔액은 `balance` 가 아니라 `credit` 이다 — 실측에서 balance 0 · credit 25.0 이었다.
    #   balance 를 읽으면 멀쩡한 계정이 "$0 · 최대 0시간" 으로 전부 막힌다.
    #
    # ⚠ **계정 이메일은 내보내지 않는다.** 게이트웨이는 `/api/ext/v1` 말고는 인증이 없어
    #   LAN 에서 :8000 에 닿는 누구나 이 응답을 읽는다. 화면이 쓰는 것은 크레딧뿐이고
    #   (잔여 시간 계산) 이메일은 아무 데도 안 쓴다 — 안 쓰는 것을 내보낼 이유가 없다.
    return {"checks": checks,
            "ready": all(c["ok"] for c in checks.values()),
            "credit": account["credit"] if account else None,
            "templates": templates}
