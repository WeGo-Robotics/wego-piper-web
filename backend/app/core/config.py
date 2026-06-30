from pydantic_settings import BaseSettings
from pathlib import Path
import json
import logging

_logger = logging.getLogger(__name__)

_MODEL_PATHS_FILE = Path.home() / ".config" / "piper-web" / "model_paths.json"


def _load_model_paths() -> list[str]:
    if _MODEL_PATHS_FILE.exists():
        try:
            return json.loads(_MODEL_PATHS_FILE.read_text())
        except Exception:
            pass
    return [str(Path.home() / ".cache" / "huggingface" / "hub")]


def _save_model_paths(paths: list[str]) -> None:
    _MODEL_PATHS_FILE.parent.mkdir(parents=True, exist_ok=True)
    _MODEL_PATHS_FILE.write_text(json.dumps(paths, indent=2))


class Settings(BaseSettings):
    # 서버
    host: str = "0.0.0.0"
    port: int = 8000
    debug: bool = False

    # CORS
    cors_origins: list[str] = ["http://localhost:5173"]

    # 경로
    models_dir: Path = Path.home() / ".cache" / "huggingface" / "hub"
    datasets_dir: Path = Path.home() / ".cache" / "huggingface" / "hub"
    lerobot_dir: Path = Path.home() / ".cache" / "huggingface" / "lerobot"

    @property
    def model_paths(self) -> list[Path]:
        return [Path(p) for p in _load_model_paths()]

    def add_model_path(self, path: str) -> list[str]:
        paths = _load_model_paths()
        if path not in paths:
            paths.append(path)
            _save_model_paths(paths)
        return paths

    def remove_model_path(self, path: str) -> list[str]:
        paths = _load_model_paths()
        paths = [p for p in paths if p != path]
        _save_model_paths(paths)
        return paths

    # ZMQ
    zmq_address: str = "tcp://127.0.0.1:5555"
    # 녹화 중 카메라 프레임 미리보기 전송 채널 (wrapper PUSH → 백엔드 PULL)
    preview_zmq_address: str = "tcp://127.0.0.1:5556"
    # 녹화 에피소드 제어 채널 (백엔드 PUSH → wrapper PULL): 건너뛰기/재녹화/정지.
    # 헤드리스라 pynput 키 주입이 불가하므로 LeRobot events dict 을 직접 set 한다.
    control_zmq_address: str = "tcp://127.0.0.1:5557"

    # E-stop
    estop_heartbeat_interval_ms: int = 500
    estop_timeout_ms: int = 2000

    # 디스크 용량 경고 임계치 (GB)
    disk_warning_threshold_gb: float = 10.0

    # 실행 경로
    local_python: str = "python"  # 로컬 wrapper용 python
    grpc_python: str = str(Path.home() / "miniconda3" / "bin" / "python")  # gRPC wrapper용 python
    hf_cli: str = ""  # huggingface-cli 경로 (빈 문자열이면 자동 탐색)

    model_config = {"env_prefix": "PIPER_", "env_file": ".env"}


settings = Settings()
