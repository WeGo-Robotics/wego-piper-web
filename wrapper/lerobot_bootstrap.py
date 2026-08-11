"""lerobot.policies.__init__ 우회 (Python 3.13 groot 호환).

`lerobot.policies.__init__` 이 `modeling_groot` 를 끌어오는데 Python 3.13 에서 깨진다.
그래서 **더미 패키지로 갈아끼우고** 필요한 config 클래스만 개별 서브모듈에서 등록한다.

이 부트스트랩이 wrapper 3개 파일에 거의 그대로 복붙돼 있었다
(refactor/03-wrapper-bootstrap.md). LeRobot 이 정책을 추가하면 세 파일을 다 고쳐야 했다.

## 쓰는 법

**다른 lerobot import 보다 먼저** import 한다 (import 순서에 민감하다):

    import lerobot_bootstrap  # noqa: F401  (side-effect import)

groot config 까지 필요하면 (start_record / start_policy_server):

    import lerobot_bootstrap
    lerobot_bootstrap.load_groot_config()

## 주의

- wrapper 는 백엔드와 **다른 파이썬**에서 돈다 (`settings.local_python` / `grpc_python`).
  백엔드 패키지를 import 하면 안 되므로 `wrapper/` 안에 둔다.
- 실행 방식이 `python /경로/wrapper/xxx.py` 라 `sys.path[0]` 가 wrapper 디렉터리다 →
  `import lerobot_bootstrap` 이 그대로 동작한다
  (`parking_controller.py` 의 `from arm_controller import ...` 가 같은 방식이다).
- 아래 `_CONFIG_IMPORTS` 는 `lerobot_wrapper.py` 의 `POLICY_IMPORTS` 와 목록이 겹치지만
  **같지 않다** (여기는 config 6개, 저기는 model+config 8개). 기존 동작을 그대로 옮긴
  것이므로 임의로 맞추지 않는다 — 바꾸려면 실기로 확인해야 한다.
"""

import importlib
import os
import sys
import types

# helpers.py 가 필요로 하는 config 클래스
_CONFIG_IMPORTS = {
    "ACTConfig": "lerobot.policies.act.configuration_act",
    "DiffusionConfig": "lerobot.policies.diffusion.configuration_diffusion",
    "PI0Config": "lerobot.policies.pi0.configuration_pi0",
    "PI05Config": "lerobot.policies.pi05.configuration_pi05",
    "SmolVLAConfig": "lerobot.policies.smolvla.configuration_smolvla",
    "VQBeTConfig": "lerobot.policies.vqbet.configuration_vqbet",
}

_lerobot = importlib.import_module("lerobot")
POLICIES_DIR = os.path.join(os.path.dirname(_lerobot.__file__), "policies")

# ── 1. lerobot.policies 를 더미 패키지로 교체 ──
policies_pkg = types.ModuleType("lerobot.policies")
policies_pkg.__path__ = [POLICIES_DIR]
policies_pkg.__package__ = "lerobot.policies"
sys.modules["lerobot.policies"] = policies_pkg

# ── 2. config 클래스 등록 (없는 정책은 조용히 건너뛴다) ──
for _cls_name, _mod_path in _CONFIG_IMPORTS.items():
    try:
        _mod = importlib.import_module(_mod_path)
        setattr(policies_pkg, _cls_name, getattr(_mod, _cls_name))
    except Exception:
        pass

def load_groot_config() -> bool:
    """groot 더미 서브패키지 등록 + config 직접 로드 (modeling_groot 는 건너뜀).

    `start_record.py` / `start_policy_server.py` 처럼 GrootConfig 가 필요한 쪽만 호출한다.
    **`grpc_wrapper.py` 는 부르지 않는다 — 원래도 이 블록이 없었다.**
    모듈 로드 시 side-effect 로 하면 grpc_wrapper 의 동작이 바뀐다.

    실패해도 예외를 올리지 않는다 (기존 동작 유지). 성공 여부를 bool 로 돌려준다.
    """
    groot_pkg = types.ModuleType("lerobot.policies.groot")
    groot_pkg.__path__ = [os.path.join(POLICIES_DIR, "groot")]
    groot_pkg.__package__ = "lerobot.policies.groot"
    sys.modules["lerobot.policies.groot"] = groot_pkg

    try:
        import importlib.util as ilu

        spec = ilu.spec_from_file_location(
            "lerobot.policies.groot.configuration_groot",
            os.path.join(POLICIES_DIR, "groot", "configuration_groot.py"),
        )
        mod = ilu.module_from_spec(spec)
        sys.modules["lerobot.policies.groot.configuration_groot"] = mod
        spec.loader.exec_module(mod)
        groot_pkg.configuration_groot = mod
        groot_pkg.GrootConfig = mod.GrootConfig
        policies_pkg.GrootConfig = mod.GrootConfig
        return True
    except Exception:
        return False
