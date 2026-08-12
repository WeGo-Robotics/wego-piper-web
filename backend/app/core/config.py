from pydantic import Field
from pydantic_settings import BaseSettings
from pathlib import Path
import json
import logging

_logger = logging.getLogger(__name__)

_BACKEND_DIR = Path(__file__).resolve().parents[2]  # backend/


def _model_paths_file(config_dir: Path) -> Path:
    return config_dir / "model_paths.json"


def _load_model_paths(config_dir: Path) -> list[str]:
    """UI에서 추가한 스캔 경로. 없으면 빈 목록 (기본 경로는 Settings.model_paths가 결정)."""
    f = _model_paths_file(config_dir)
    if f.exists():
        try:
            return json.loads(f.read_text())
        except Exception:
            _logger.warning("model_paths.json 파싱 실패, 무시함: %s", f)
    return []


def _save_model_paths(config_dir: Path, paths: list[str]) -> None:
    f = _model_paths_file(config_dir)
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps(paths, indent=2))


class Settings(BaseSettings):
    # 서버
    host: str = "0.0.0.0"
    port: int = 8000
    debug: bool = False

    # 이 기기의 로봇 식별자. 로봇마다 별도 인스턴스로 배포하므로 기기별 설정이다.
    # 데이터셋 repo 네이밍({org}/{robot_id}_{task}_{date})과 에피소드 메타데이터가
    # 이 값을 쓴다 — 없으면 사람이 매번 손으로 정하게 되고 반드시 틀린다.
    # 비워두면 호스트명을 쓴다.
    robot_id: str = ""

    # HuggingFace API 엔드포인트. 비우면 huggingface.co.
    # 사내 자체 Hub(HF API 호환)로 옮길 때 이 하나만 바꾸면 백엔드와
    # **LeRobot subprocess 까지** 함께 방향이 바뀐다 — huggingface_hub 가
    # HF_ENDPOINT 환경변수를 기본으로 읽기 때문이다 (ROADMAP 참고).
    hf_endpoint: str = ""

    # CORS
    cors_origins: list[str] = ["http://localhost:5173"]

    # ── 경로 ──
    # 기본값은 호스트(직접 실행) 기준. 도커에서는 compose가 /data/* 로 덮어쓴다.
    # 절대경로만 사용한다 (CWD가 바뀌어도 같은 곳을 가리켜야 함).
    models_dir: Path = Path.home() / ".cache" / "huggingface" / "hub"
    datasets_dir: Path = Path.home() / ".cache" / "huggingface" / "hub"
    lerobot_dir: Path = Path.home() / ".cache" / "huggingface" / "lerobot"

    # 사용자 설정/세션 (model_paths.json, 프리셋, 카메라·로봇 세션)
    config_dir: Path = Path.home() / ".config" / "piper-web"
    # 로봇 CAN 설정. config_dir 와 별개인 이유는 기존 호스트 파일 위치를 유지하기 위함.
    robot_config_path: Path = Path.home() / "piper_config.json"
    # 앱이 생성하는 로그/기록 (eval_logs, debug)
    log_dir: Path = _BACKEND_DIR / "data"

    # 모델 스캔 루트. ':' 로 구분 (PATH 형식). 비우면 models_dir 하나만 스캔.
    # 필드명이 model_ 로 시작하면 pydantic 보호 네임스페이스와 충돌하므로 alias 로 매핑.
    scan_paths: str = Field(default="", validation_alias="PIPER_MODEL_PATHS")

    @property
    def model_paths(self) -> list[Path]:
        """스캔 루트 = 환경변수(또는 models_dir) + UI에서 추가한 경로."""
        base = [p for p in self.scan_paths.split(":") if p] or [str(self.models_dir)]
        merged = list(dict.fromkeys(base + _load_model_paths(self.config_dir)))
        return [Path(p) for p in merged]

    def add_model_path(self, path: str) -> list[str]:
        paths = _load_model_paths(self.config_dir)
        if path not in paths:
            paths.append(path)
            _save_model_paths(self.config_dir, paths)
        return paths

    def remove_model_path(self, path: str) -> list[str]:
        paths = _load_model_paths(self.config_dir)
        paths = [p for p in paths if p != path]
        _save_model_paths(self.config_dir, paths)
        return paths

    # 카메라 전송 방식 (refactor/camera-transport.md).
    #
    #   "direct" — wrapper 가 v4l2/RealSense 를 **직접 연다** (지금까지의 방식).
    #              그래서 추론 전에 웹이 쥔 카메라를 해제해야 하고,
    #              컨테이너는 privileged + /dev 마운트가 필요하다.
    #   "shm"    — 발행자가 장치를 독점하고 wrapper 는 `/dev/shm` 에서 픽셀만 읽는다.
    #              해제 춤이 사라지고 JPEG 이중압축도 없다.
    #
    # 기본값이 "direct" 인 이유: **실기로 fps·지연을 비교한 뒤에 바꾼다.**
    # 되돌리기가 이 값 하나로 끝나야 한다.
    camera_transport: str = "direct"

    # 버스 (Redis) — ZMQ 소켓 3개(5555 파라미터 / 5556 프리뷰 / 5557 녹화제어)를
    # 대체한다 (refactor/daemon-split.md 3단계). 주소가 3개에서 1개로 줄었다.
    # 비워두면 `piper_bus` 기본값(`PIPER_REDIS_URL` 또는 localhost:6379/0)을 쓴다.
    redis_url: str = ""

    # E-stop
    estop_heartbeat_interval_ms: int = 500
    estop_timeout_ms: int = 2000

    # 디스크 용량 경고 임계치 (GB)
    disk_warning_threshold_gb: float = 10.0

    # 디버그 모드에서 추론 데이터를 기록할 폴더 (wrapper에 PIPER_DEBUG_DIR로 전달)
    debug_dir: str = "/tmp/piper_debug"

    # 실행 경로
    local_python: str = "python"  # 로컬 wrapper용 python
    grpc_python: str = str(Path.home() / "miniconda3" / "bin" / "python")  # gRPC wrapper용 python
    hf_cli: str = ""  # huggingface-cli 경로 (빈 문자열이면 자동 탐색)

    @property
    def resolved_robot_id(self) -> str:
        """설정된 robot_id, 없으면 호스트명."""
        if self.robot_id:
            return self.robot_id
        import socket
        return socket.gethostname()

    # env_file 도 절대경로로. 상대경로면 실행 CWD 에 따라 .env 가 조용히 무시된다.
    model_config = {"env_prefix": "PIPER_", "env_file": str(_BACKEND_DIR / ".env")}


settings = Settings()
