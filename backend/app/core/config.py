from pydantic_settings import BaseSettings
from pathlib import Path


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

    # ZMQ
    zmq_address: str = "tcp://127.0.0.1:5555"

    # E-stop
    estop_heartbeat_interval_ms: int = 500
    estop_timeout_ms: int = 2000

    # 디스크 용량 경고 임계치 (GB)
    disk_warning_threshold_gb: float = 10.0

    model_config = {"env_prefix": "PIPER_", "env_file": ".env"}


settings = Settings()
