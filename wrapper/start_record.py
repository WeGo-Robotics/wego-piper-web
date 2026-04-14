"""
레코딩 래퍼 (lerobot 0.5 / python3.13).
lerobot.policies.__init__ 우회 후 lerobot-record를 실행.

사용법:
  python start_record.py --robot.type=piper_follower --robot.port=can_follower1 ...
"""

import sys
import types
import importlib
import os

# ── lerobot.policies.__init__.py 우회 (Python 3.13 groot 호환성) ──
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

# 플러그인 등록
from lerobot.utils.import_utils import register_third_party_plugins
register_third_party_plugins()

# ── 레코딩 실행 ──
from lerobot.scripts.lerobot_record import record

if __name__ == "__main__":
    record()
