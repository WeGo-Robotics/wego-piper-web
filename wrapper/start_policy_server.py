"""
정책 서버 시작 래퍼 (lerobot 0.5 / python3.13).
lerobot.policies.__init__ 우회 후 policy_server를 실행.

사용법:
  python start_policy_server.py --host=127.0.0.1 --port=8088 --fps=30
"""

import sys
import types
import importlib
import os

# ── lerobot.policies.__init__.py 우회 ──
_lerobot = importlib.import_module("lerobot")
_policies_dir = os.path.join(os.path.dirname(_lerobot.__file__), "policies")
_pkg = types.ModuleType("lerobot.policies")
_pkg.__path__ = [_policies_dir]
_pkg.__package__ = "lerobot.policies"
sys.modules["lerobot.policies"] = _pkg

# helpers.py가 필요로 하는 config 클래스 등록
_config_imports = {
    "ACTConfig": "lerobot.policies.act.configuration_act",
    "DiffusionConfig": "lerobot.policies.diffusion.configuration_diffusion",
    "PI0Config": "lerobot.policies.pi0.configuration_pi0",
    "PI05Config": "lerobot.policies.pi05.configuration_pi05",
    "SmolVLAConfig": "lerobot.policies.smolvla.configuration_smolvla",
    "VQBeTConfig": "lerobot.policies.vqbet.configuration_vqbet",
}
for _cls_name, _mod_path in _config_imports.items():
    try:
        _mod = importlib.import_module(_mod_path)
        setattr(_pkg, _cls_name, getattr(_mod, _cls_name))
    except Exception:
        pass

# groot 서브패키지를 더미로 등록하여 modeling_groot import 방지
_groot_pkg = types.ModuleType("lerobot.policies.groot")
_groot_pkg.__path__ = [os.path.join(_policies_dir, "groot")]
_groot_pkg.__package__ = "lerobot.policies.groot"
sys.modules["lerobot.policies.groot"] = _groot_pkg

# groot config만 직접 로드 (modeling_groot는 건너뜀)
try:
    import importlib.util as _ilu
    _groot_cfg_spec = _ilu.spec_from_file_location(
        "lerobot.policies.groot.configuration_groot",
        os.path.join(_policies_dir, "groot", "configuration_groot.py"),
    )
    _groot_cfg_mod = _ilu.module_from_spec(_groot_cfg_spec)
    sys.modules["lerobot.policies.groot.configuration_groot"] = _groot_cfg_mod
    _groot_cfg_spec.loader.exec_module(_groot_cfg_mod)
    _groot_pkg.configuration_groot = _groot_cfg_mod
    _groot_pkg.GrootConfig = _groot_cfg_mod.GrootConfig
    _pkg.GrootConfig = _groot_cfg_mod.GrootConfig
except Exception:
    pass

# 정책별 프로세서 등록 (ProcessorStepRegistry에 등록)
try:
    import lerobot.policies.smolvla.processor_smolvla  # noqa: F401
except Exception:
    pass
try:
    import lerobot.policies.pi0.processor_pi0  # noqa: F401
except Exception:
    pass
try:
    import lerobot.policies.pi05.processor_pi05  # noqa: F401
except Exception:
    pass

# ── 모델 캐싱 서버 (상속 패치) ──
import logging
import pickle  # nosec
import time

from lerobot.async_inference.policy_server import PolicyServer, serve as _original_serve
from lerobot.async_inference.helpers import RemotePolicyConfig
from lerobot.policies.factory import get_policy_class, make_pre_post_processors
from lerobot.transport import services_pb2


class CachedPolicyServer(PolicyServer):
    """모델이 변경되지 않으면 재로딩을 건너뛰는 PolicyServer."""

    def __init__(self, config):
        super().__init__(config)
        self._loaded_path: str | None = None
        self._loaded_type: str | None = None
        self._loaded_device: str | None = None

    def SendPolicyInstructions(self, request, context):  # noqa: N802
        if not self.running:
            self.logger.warning("Server is not running. Ignoring policy instructions.")
            return services_pb2.Empty()

        policy_specs = pickle.loads(request.data)  # nosec
        if not isinstance(policy_specs, RemotePolicyConfig):
            raise TypeError(f"Expected RemotePolicyConfig, got {type(policy_specs)}")

        same_model = (
            self._loaded_path == policy_specs.pretrained_name_or_path
            and self._loaded_type == policy_specs.policy_type
            and self._loaded_device == policy_specs.device
            and self.policy is not None
        )

        self.lerobot_features = policy_specs.lerobot_features
        self.actions_per_chunk = policy_specs.actions_per_chunk

        if same_model:
            self.logger.info(
                f"Model unchanged ({self._loaded_path}), skipping reload. "
                f"Updated: actions_per_chunk={policy_specs.actions_per_chunk}"
            )
            return services_pb2.Empty()

        # 새 모델 로딩
        self.logger.info(
            f"Loading policy: {policy_specs.policy_type} | "
            f"Path: {policy_specs.pretrained_name_or_path} | "
            f"Device: {policy_specs.device}"
        )
        self.device = policy_specs.device
        self.policy_type = policy_specs.policy_type

        policy_class = get_policy_class(self.policy_type)
        start = time.perf_counter()
        self.policy = policy_class.from_pretrained(policy_specs.pretrained_name_or_path)
        self.policy.to(self.device)

        device_override = {"device": self.device}
        self.preprocessor, self.postprocessor = make_pre_post_processors(
            self.policy.config,
            pretrained_path=policy_specs.pretrained_name_or_path,
            preprocessor_overrides={
                "device_processor": device_override,
                "rename_observations_processor": {"rename_map": policy_specs.rename_map},
            },
            postprocessor_overrides={"device_processor": device_override},
        )
        elapsed = time.perf_counter() - start

        self._loaded_path = policy_specs.pretrained_name_or_path
        self._loaded_type = policy_specs.policy_type
        self._loaded_device = policy_specs.device

        self.logger.info(f"Policy loaded in {elapsed:.2f}s on {self.device}")
        return services_pb2.Empty()


def serve_cached():
    """CachedPolicyServer로 서버 시작."""
    import grpc
    from concurrent import futures
    from dataclasses import asdict
    from pprint import pformat
    import draccus
    from lerobot.async_inference.configs import PolicyServerConfig

    @draccus.wrap()
    def _main(cfg: PolicyServerConfig):
        logging.info(pformat(asdict(cfg)))
        policy_server = CachedPolicyServer(cfg)
        server = grpc.server(futures.ThreadPoolExecutor(max_workers=4))
        from lerobot.transport import services_pb2_grpc
        services_pb2_grpc.add_AsyncInferenceServicer_to_server(policy_server, server)
        server.add_insecure_port(f"{cfg.host}:{cfg.port}")
        policy_server.logger.info(f"CachedPolicyServer started on {cfg.host}:{cfg.port}")
        server.start()
        server.wait_for_termination()

    _main()


if __name__ == "__main__":
    serve_cached()
