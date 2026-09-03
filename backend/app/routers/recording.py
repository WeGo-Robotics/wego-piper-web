"""데이터셋 레코딩 API."""

import logging

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.core.cli_mapping import build_record_args
from app.core.hf_layout import repo_id_error
from app.core.config import settings
from app.services.exclusivity import Activity, require_idle
from app.services.record_manager import record_manager

# 정지 시점에 사이드카를 쓰려면 시작 때의 매핑이 필요하다.
# 재시작하면 비지만, 그때는 녹화도 없으므로 남길 것도 없다.
_last_recording: dict = {}

router = APIRouter(prefix="/api/recording", tags=["recording"])
logger = logging.getLogger(__name__)


class RecordStartRequest(BaseModel):
    robot_type: str = "piper_follower"
    robot_port: str = ""
    # ── 양팔 (feature/bimanual.md 2단계) ──
    # [왼팔, 오른팔] 순서 고정 — 둘 다 있으면 양팔 모드이고 robot_type 은
    # bi_piper_follower 를 기대한다. 카메라는 camera_mapping 의 키 접두사로
    # 팔에 배정한다: `left_*`→왼팔, `right_*`→오른팔, 나머지(공용)→왼팔.
    robot_ports: list[str] = []
    teleop_ports: list[str] = []
    robot_cameras: dict = {}
    # `{카메라키: 장치id}`. 주면 백엔드가 `--robot.cameras` JSON 을 조립한다.
    # 프론트가 조립하면 백엔드 설정(`camera_transport`)을 몰라 **녹화만 옛 경로**를 탄다.
    camera_mapping: dict[str, str] = {}
    # 카메라 요청 해상도·fps (`direct` 에서만 쓰인다 — `shm` 은 발행자가 정한다)
    camera_width: int = 0
    camera_height: int = 0
    camera_fps: int = 0
    # 데이터셋 설명 — 정지 시 meta/piper_notes.json 사이드카로 남는다.
    # LeRobot info.json 에는 이 자리가 없다 (notes_sidecar 참고).
    description: str = ""
    # 시작 전에 1회 적용할 카메라 프로파일 (노출·WB — presets domain=camera).
    # 빈 값이면 적용하지 않는다. 프로파일이 없는 이름이면 시작을 거부한다.
    camera_profile: str = ""
    teleop_type: str = "piper_leader"
    teleop_port: str = ""
    repo_id: str = ""
    single_task: str = ""
    num_episodes: int = 50
    fps: int = 15
    episode_time_s: int = 60
    reset_time_s: int = 60
    streaming_encoding: bool = True
    vcodec: str = "auto"
    encoder_threads: int = 4
    encoder_queue_maxsize: int = 100
    push_to_hub: bool = True
    private: bool = False
    resume: bool = False
    web_preview: bool = True  # 녹화 중 웹 카메라 미리보기 (log_rerun_data 탭)


class RecordPreviewRequest(BaseModel):
    robot_type: str = "piper_follower"
    robot_port: str = ""
    robot_ports: list[str] = []
    teleop_ports: list[str] = []
    robot_cameras: dict = {}
    camera_mapping: dict[str, str] = {}
    camera_width: int = 0
    camera_height: int = 0
    camera_fps: int = 0
    teleop_type: str = "piper_leader"
    teleop_port: str = ""
    repo_id: str = ""
    single_task: str = ""
    num_episodes: int = 50
    fps: int = 15
    episode_time_s: int = 60
    reset_time_s: int = 60
    streaming_encoding: bool = True
    vcodec: str = "auto"
    encoder_threads: int = 4
    encoder_queue_maxsize: int = 100
    push_to_hub: bool = True
    resume: bool = False


def _split_camera_mapping(mapping: dict[str, str]) -> tuple[dict[str, str], dict[str, str]]:
    """카메라를 팔에 배정한다. 키 접두사가 곧 배정이고, 접두사는 벗겨서 넣는다 —
    bi 클래스가 관측 키에 팔 접두사를 도로 붙이므로 `left_hand` → 왼팔 `hand`
    → 관측 `left_hand` 로 왕복이 맞는다. 공용(무접두사)은 왼팔 소속
    (grpc 즉석 조립이 확립한 규약, feature/bimanual.md §2)."""
    left, right = {}, {}
    for name, cam_id in mapping.items():
        if name.startswith("right_"):
            right[name.removeprefix("right_")] = cam_id
        elif name.startswith("left_"):
            left[name.removeprefix("left_")] = cam_id
        else:
            left[name] = cam_id
    return left, right


def _apply_arm_params(params: dict, *, cam_w: int, cam_h: int, cam_fps: int) -> dict:
    """단팔/양팔에 따라 포트·카메라 키 집합을 조립한다.

    시작과 미리보기가 **같은 조립기**를 써야 미리보기가 거짓말을 안 한다.
    양팔이면 단수 키(robot_port/teleop_port)를 지우고 중첩 키만 남긴다 —
    섞이면 draccus 가 `--robot.port` 를 bi 설정에 넣으려다 죽는다.
    """
    from app.services.camera_config import build_cameras_json

    mapping = params.pop("camera_mapping", None) or {}
    robot_ports = params.pop("robot_ports", None) or []
    teleop_ports = params.pop("teleop_ports", None) or []

    if len(robot_ports) >= 2:
        params.pop("robot_port", None)
        params.pop("teleop_port", None)
        # ⚠ **양팔 설정에는 최상위 `cameras` 가 없다.** 카메라는 팔별
        #   (`left_arm_config.cameras`)로 들어간다. 안 지우면 빈 dict 라도
        #   `--robot.cameras={}` 가 나가고 draccus 가
        #   "The fields `cameras` are not valid for BiPiperShmFollowerConfig" 로
        #   죽는다 — 카메라를 하나도 안 붙여도 그렇다.
        params.pop("robot_cameras", None)
        params["left_robot_port"], params["right_robot_port"] = robot_ports[0], robot_ports[1]
        if len(teleop_ports) >= 2:
            params["left_teleop_port"], params["right_teleop_port"] = teleop_ports[0], teleop_ports[1]
        left_map, right_map = _split_camera_mapping(mapping)
        if left_map:
            params["left_robot_cameras"] = build_cameras_json(
                left_map, width=cam_w, height=cam_h, fps=cam_fps)
        if right_map:
            params["right_robot_cameras"] = build_cameras_json(
                right_map, width=cam_w, height=cam_h, fps=cam_fps)
    elif mapping:
        params["robot_cameras"] = build_cameras_json(
            mapping, width=cam_w, height=cam_h, fps=cam_fps)
    return params


@router.post("/start")
async def start_recording(body: RecordStartRequest):
    """녹화 시작."""
    require_idle(Activity.RECORDING)
    # LeRobot 은 `repo_id.split("/")` 를 2개로 언패킹한다 — 슬래시가 없으면
    # 팔·카메라를 다 잡은 뒤 ValueError 로 죽어서 원인을 알기 어렵다. 시작 전에 막는다.
    if err := repo_id_error(body.repo_id):
        raise HTTPException(400, err)
    if not body.single_task:
        raise HTTPException(400, "Task 설명이 필요합니다.")
    bimanual = len(body.robot_ports) >= 2
    if bimanual:
        # 타입과 포트 수가 어긋난 채 시작하면 draccus 안쪽에서 죽어 원인을 알기 어렵다
        from app.core.cli_mapping import BIMANUAL_ROBOT_TYPES

        if body.robot_type not in BIMANUAL_ROBOT_TYPES:
            raise HTTPException(400, f"양팔 포트 2개에는 bi 로봇 타입이 필요합니다 (지금: {body.robot_type})")
        if len(body.teleop_ports) < 2:
            raise HTTPException(400, "양팔 녹화에는 Leader 포트 2개가 필요합니다.")
    else:
        if not body.robot_port:
            raise HTTPException(400, "Follower 포트가 필요합니다.")
        if not body.teleop_port:
            raise HTTPException(400, "Leader 포트가 필요합니다.")

    # 전송 방식에 맞게 카메라 준비. `direct` 는 해제하고 `shm` 은 붙잡는다 —
    # 두 방식이 정반대라 한 곳(`camera_config`)에서 판단한다.
    from app.services.camera_config import (
        CameraPrepareError,
        check_camera_config,
        prepare_cameras,
    )

    # 낡은 프론트가 보낸 장치 설정을 그대로 넘기면 데몬이 쥔 장치를 또 열려다
    # LeRobot 3겹 안쪽에서 `Device or resource busy` 로 죽는다.
    if err := check_camera_config(body.robot_cameras):
        raise HTTPException(400, err)

    cam_w = body.camera_width or 0
    cam_h = body.camera_height or 0
    cam_fps = body.camera_fps or body.fps

    # ⚠ 요청 프로파일을 **여기서** 넘겨야 장치에 반영된다. 안 넘기면 데몬이
    # 기본값으로 열고(D405 는 848x480@10), 녹화 루프가 그 10Hz 에 묶인다.
    try:
        prepare_cameras(body.camera_mapping, purpose="recording",
                        width=cam_w, height=cam_h, fps=cam_fps)
    except CameraPrepareError as e:
        raise HTTPException(400, str(e))

    # 작업 프로파일 — 수집은 노출·색이 곧 데이터셋 품질이다. 지정했으면 연결
    # **뒤에** 한 번 적용하고 시작한다 (feature/lighting-watch.md 의 색 일관성
    # 문제의식과 같은 뿌리). 프로파일이 없으면 시작하지 않는다 — 기준 없이
    # 찍힌 에피소드가 조용히 섞이는 것보다 낫다.
    profile_report: dict | None = None
    if body.camera_profile:
        import asyncio

        from app.services import camera_profiles

        profile_report = await asyncio.to_thread(
            camera_profiles.apply_for_task, body.camera_profile)
        if profile_report.get("error"):
            raise HTTPException(
                400, f"카메라 프로파일 '{body.camera_profile}': {profile_report['error']}")

    # 팔도 같은 순서로 준비한다. `shm` 에서는 게이트웨이가 CAN 을 쥔 채로
    # 상태를 발행해야 프록시 드라이버가 붙는다.
    from app.services.robot_config import ArmPrepareError, prepare_arms
    from app.services.robot_manager import robot_manager

    arm_ports = (body.robot_ports + body.teleop_ports) if bimanual \
        else [body.robot_port, body.teleop_port]
    try:
        prepare_arms(arm_ports, purpose="recording")
    except ArmPrepareError as e:
        raise HTTPException(400, str(e))

    # ⚠ 0x150 리셋을 수집 시작 루틴에 넣는다 (piper_sdk #120). 텔레옵 수집이
    # 관절 슬립의 최다 트리거 환경인데, 슬립은 피드백이 거짓말해서 평소엔 안
    # 보인다 — 리셋이 보고 프레임을 실제에 재동기화하고 그 간극(=쌓인 슬립)을
    # 드러낸다. **follower 만** 리셋한다: 리더는 마스터 모드라 0x150 이 모드를
    # 흔들 수 있고(#35 계열), 리더 슬립은 사람 손이 기준이라 덜 치명적이다.
    # 실패해도 시작은 막지 않는다 — 리셋 없는 수집이 수집 못 하는 것보다 낫다.
    slip_warns: list[str] = []
    try:
        from app.services import robot_manager as robot_manager_mod
        from app.services.robot_manager import robot_manager

        followers = body.robot_ports if bimanual else [body.robot_port]
        report = robot_manager.clear_arm_errors([f for f in followers if f])
        slip_warns = robot_manager_mod.slip_warnings(report)
    except Exception as exc:
        logger.warning("녹화 시작 리셋 실패 (수집은 계속한다): %s", exc)

    # ⚠ **모드가 어긋나면 조용히 안 움직인다.** 팔로워가 마스터 모드면 외부 명령을
    #   통째로 무시해서, 리더를 아무리 끌어도 팔로워가 안 따라온다 — 녹화는 정상으로
    #   보이고 에피소드만 못 쓰게 된다. 실기에서 그렇게 하나를 버렸다.
    #   전원이 나가거나 과부하로 멈추면 모드가 풀리므로 **시작할 때마다** 본다.
    for iface in arm_ports:
        arm = robot_manager.arms.get(iface) if iface else None
        bad = arm.mode_mismatch() if arm else None
        if bad:
            raise HTTPException(409, bad)

    from app.services.control_bridge import control_bridge
    from app.services.preview_bridge import preview_bridge

    params = body.model_dump()
    params.pop("web_preview", None)
    params.pop("camera_width", None)
    params.pop("camera_height", None)
    params.pop("camera_fps", None)
    params = _apply_arm_params(params, cam_w=cam_w, cam_h=cam_h, cam_fps=cam_fps)

    # 헤드리스 에피소드 제어 채널은 미리보기와 무관하게 항상 켠다.
    # 버스 주소는 ProcessManager 가 모든 자식에게 넣으므로 여기서 넘기지 않는다.
    env_extra: dict[str, str] = {}
    control_bridge.start()

    # 웹 미리보기: display_data=true 로 log_rerun_data 호출을 켜고, wrapper 가
    # 그 프레임을 JPEG 로 버스에 올리도록 켠다.
    if body.web_preview:
        params["display_data"] = True
        env_extra["PIPER_PREVIEW"] = "1"
        preview_bridge.start()

    args = build_record_args(params)

    # ⚠ 정지 시점에는 매핑이 없다 — 지금 붙잡아 둔다. 사이드카는 데이터셋이
    # 만들어진 **뒤**에야 쓸 수 있어서 시작 때 바로 못 남긴다.
    _last_recording["repo_id"] = body.repo_id
    _last_recording["camera_mapping"] = dict(body.camera_mapping or {})
    _last_recording["description"] = body.description

    try:
        await record_manager.start(args, total_episodes=body.num_episodes, env_extra=env_extra)
    except Exception as e:
        control_bridge.stop()
        preview_bridge.stop()
        raise HTTPException(500, f"녹화 시작 실패: {e}")
    # 프로파일 경고(안 덮는 카메라 등)와 팔 슬립 경고는 막을 일은 아니지만
    # **시작 전에 알아야** 한다 — 화면이 응답에서 꺼내 시스템 메시지로 띄운다.
    return {"status": "started", "pid": record_manager.pm.pid, "args": args,
            "camera_profile": profile_report,
            "arm_reset": {"warnings": slip_warns}}


# `escape` 를 보낸 뒤 LeRobot 이 스스로 끝날 때까지 기다리는 시간.
#
# ⚠ **2초는 너무 짧았다.** `escape` 는 "지금 에피소드를 마무리하고 끝내라"는 뜻이라,
# LeRobot 은 프레임을 데이터셋에 쓰고 **비디오를 인코딩**한 뒤에야 종료한다.
# 60초 에피소드면 카메라당 900프레임이라 2초 안에 못 끝낸다 — 그 상태로 SIGTERM 을
# 보내면 인코딩 도중에 끊긴다. 실측에서도 `escape` 후 종료까지 7초가 걸렸고,
# 그 사이(2초 시점)에 SIGTERM 이 들어갔다.
GRACEFUL_STOP_S = 60


@router.post("/stop")
async def stop_recording():
    """녹화 정지. `escape` 로 정상 종료를 요청하고, 안 끝나면 프로세스를 내린다."""
    import asyncio

    record_manager.send_key("escape")

    # 스스로 끝나면 그 즉시 빠져나온다 — 다 기다리지 않는다.
    deadline = GRACEFUL_STOP_S * 4
    for _ in range(deadline):
        if not record_manager.is_running:
            break
        await asyncio.sleep(0.25)

    graceful = not record_manager.is_running
    if not graceful:
        logger.warning(
            "escape 후 %d초 안에 끝나지 않아 프로세스를 종료합니다", GRACEFUL_STOP_S
        )
        await record_manager.stop()

    from app.services.control_bridge import control_bridge
    from app.services.preview_bridge import preview_bridge
    control_bridge.stop()
    preview_bridge.stop()

    # 카메라 해석에 필요한 값을 데이터셋 옆에 남긴다. LeRobot 은 `meta/info.json` 에
    # 카메라 설정을 안 적어서, 깊이 인코딩 범위 같은 건 여기 없으면 영영 모른다.
    from app.services.camera_config import write_camera_sidecar

    repo_id = _last_recording.get("repo_id") or ""
    mapping = _last_recording.get("camera_mapping") or {}
    if repo_id and mapping:
        write_camera_sidecar(settings.lerobot_dir / repo_id, mapping)

    # 설명도 같은 시점에 사이드카로. 비어 있으면 안 쓴다 — 기존 설명을 빈 값으로
    # 덮는 사고를 막는다. 실패해도 정지 흐름은 막지 않는다 (카메라 사이드카와 동일).
    desc = (_last_recording.get("description") or "").strip()
    if repo_id and desc:
        try:
            from app.services.notes_sidecar import write_notes
            write_notes(settings.lerobot_dir / repo_id, kind="dataset",
                        name="", description=desc)
        except Exception as exc:
            logger.warning("설명 사이드카 기록 실패 (%s): %s", repo_id, exc)

    return {"status": "stopped", "graceful": graceful}


class TaskRequest(BaseModel):
    task: str


@router.post("/task")
async def set_task(body: TaskRequest):
    """녹화 중 task 문구 변경. **다음 에피소드부터** 적용된다.

    LeRobot 은 에피소드 시작 시점의 task 를 그 에피소드의 모든 프레임에 찍는다.
    진행 중인 에피소드를 도중에 바꾸면 한 에피소드 안에서 프레임마다 task 가 달라져
    "에피소드 = 하나의 task" 전제가 깨지므로, 경계에서만 바꾼다.
    """
    task = body.task.strip()
    if not task:
        raise HTTPException(400, "task 가 비어 있습니다")
    from app.services.control_bridge import control_bridge
    if not control_bridge.set_task(task):
        raise HTTPException(409, "녹화 중이 아니거나 버스에 연결되지 않았습니다")
    return {"status": "ok", "task": task, "applies_from": "next_episode"}


@router.get("/preview")
async def list_preview_cameras():
    """녹화 중 미리보기 가능한 카메라 이름 목록 (최근 프레임이 있는 것만)."""
    from app.services.preview_bridge import preview_bridge
    return {"cameras": preview_bridge.names()}


@router.get("/preview-stream/{name}")
async def preview_stream(name: str, fps: float = 10.0):
    """녹화 프리뷰 스트림.

    ⚠ 프레임 출처가 카메라 페이지와 다르다 — 여기는 wrapper 가 버스에 올린
    JPEG 이라 이미 인코딩돼 있다. 새 프레임 판정도 `seq` 가 아니라 **바이트
    비교**다. wrapper 쪽 상한이 10fps 라 여기서 더 올려봐야 같은 것만 다시 온다.
    """
    from app.services import mjpeg
    from app.services.preview_bridge import preview_bridge

    last = {"jpeg": None}

    def _next() -> bytes | None:
        cur = preview_bridge.get(name)
        if cur is None or cur == last["jpeg"]:
            return None
        last["jpeg"] = cur
        return cur

    return mjpeg.stream(_next, label=f"record:{name}", fps=fps)


@router.get("/preview/{name}")
async def get_preview_frame(name: str):
    """녹화 중 카메라 최신 프레임 (단일 JPEG)."""
    from fastapi.responses import Response
    from app.services.preview_bridge import preview_bridge
    data = preview_bridge.get(name)
    if data is None:
        raise HTTPException(404, "Preview unavailable")
    return Response(content=data, media_type="image/jpeg")


@router.post("/skip")
async def skip_episode():
    """이번 에피소드를 지금 마감하고 **저장**한 뒤 다음으로 (→ 키).

    ⚠ 건너뛰기가 아니다 — LeRobot 이 `save_episode()` 로 떨어진다.
    리셋 대기 중이면 "리셋 끝, 다음 시작"이 된다. 버리려면 `/rerecord` 를 쓴다.
    """
    if not record_manager.is_running:
        raise HTTPException(400, "녹화가 실행 중이 아닙니다.")
    record_manager.send_key("right")
    return {"status": "skipped"}


@router.post("/rerecord")
async def rerecord_episode():
    """현재 에피소드 재녹화 (← 키 주입)."""
    if not record_manager.is_running:
        raise HTTPException(400, "녹화가 실행 중이 아닙니다.")
    record_manager.send_key("left")
    return {"status": "rerecording"}


@router.get("/status")
async def recording_status():
    """녹화 상태."""
    return record_manager.get_status()


@router.get("/check-dataset/{repo_id:path}")
async def check_dataset_exists(repo_id: str):
    """데이터셋 로컬 존재 여부 확인."""
    dataset_path = settings.lerobot_dir / repo_id
    exists = dataset_path.exists()
    size_mb = 0.0
    if exists:
        size_mb = round(sum(f.stat().st_size for f in dataset_path.rglob("*") if f.is_file()) / (1024 * 1024), 1)
    return {"exists": exists, "path": str(dataset_path), "size_mb": size_mb}


@router.delete("/delete-dataset/{repo_id:path}")
async def delete_dataset_for_recording(repo_id: str):
    """레코딩용 데이터셋 삭제."""
    import shutil
    dataset_path = settings.lerobot_dir / repo_id
    if not dataset_path.exists():
        raise HTTPException(404, "데이터셋이 없습니다.")
    shutil.rmtree(dataset_path)
    logger.info("Deleted dataset for re-recording: %s", dataset_path)
    return {"status": "deleted", "path": str(dataset_path)}


@router.post("/preview")
async def preview_record_args(body: RecordPreviewRequest):
    """녹화 CLI 인자 미리보기. 시작과 **같은 조립기**를 써야 미리보기가 거짓말을 안 한다."""
    params = body.model_dump()
    cam_w = params.pop("camera_width", 0)
    cam_h = params.pop("camera_height", 0)
    cam_fps = params.pop("camera_fps", 0) or body.fps
    params = _apply_arm_params(params, cam_w=cam_w, cam_h=cam_h, cam_fps=cam_fps)
    args = build_record_args(params)
    return {"args": args, "command": " ".join(args)}
