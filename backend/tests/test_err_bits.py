"""`_ERR_BITS` 프로세스 경계 복붙 (refactor/04-err-bits.md).

Piper `err_code` 비트 → 의미 매핑이 백엔드와 wrapper 에 각각 있다.
**안전 관련 표시**라 어긋나면 엉뚱한 관절을 지목하거나 에러를 놓친다.

문서는 (a) "wrapper 에 두고 백엔드가 읽기" 를 권했지만 (c) 테스트 고정을 택했다:

- 백엔드가 `wrapper/` 를 import 하려면 `sys.path` 조작이 필요하다 (선례가 없다)
- `_ERR_BITS` 는 비공개 이름이라 모듈 API 로 삼기에 부적절하다
- daemon-split 후 robotd 가 CAN 을 독점하면 wrapper 쪽 사본이 사라진다 —
  지금 런타임 결합을 만들면 그때 되돌려야 한다

즉 **복제를 유지하되 어긋나면 여기서 잡는다.** 드리프트 위험은 (a) 와 동일하게 없어진다.
"""

import re
from pathlib import Path

from app.services.robot_manager import ArmInfo

_REPO = Path(__file__).resolve().parents[2]
_ARM_CONTROLLER = _REPO / "wrapper" / "arm_controller.py"


def _wrapper_err_bits() -> dict[int, str]:
    src = _ARM_CONTROLLER.read_text()
    block = re.search(r"_ERR_BITS = \{(.*?)\n\}", src, re.S)
    assert block, "arm_controller.py 에서 _ERR_BITS 를 못 찾았다"
    return {int(b): n for b, n in re.findall(r"(\d+):\s*\"(\w+)\"", block.group(1))}


def test_err_bits_match_across_process_boundary():
    """백엔드와 wrapper 의 비트 매핑이 같아야 한다.

    wrapper 주석이 이미 *"백엔드 robot_manager._ERR_BITS와 동일"* 이라고 적고 있다 —
    복제라는 것을 알면서 둔 상태였다.
    """
    assert ArmInfo._ERR_BITS == _wrapper_err_bits(), (
        "백엔드와 wrapper 의 _ERR_BITS 가 다르다 — 에러 플래그가 조용히 잘못 표시된다"
    )


def test_err_bits_cover_all_six_joints():
    """관절 6개 × (통신 / 각도 한계) 가 빠짐없이 있어야 한다."""
    names = set(ArmInfo._ERR_BITS.values())
    for i in range(1, 7):
        assert f"joint{i}_comm" in names, f"joint{i}_comm 누락"
        assert f"joint{i}_angle_limit" in names, f"joint{i}_angle_limit 누락"


def test_fault_flags_are_wrapper_only():
    """`_FAULT_FLAGS` 는 아직 wrapper 에만 있다.

    백엔드에도 같은 개념이 필요해지면 **여기서부터 같은 복붙 문제가 시작된다** —
    그때는 이 테스트를 위 `_ERR_BITS` 와 같은 형태로 확장한다 (문서 "함께 볼 것").
    """
    src = _ARM_CONTROLLER.read_text()
    assert "_FAULT_FLAGS" in src
    backend_src = (_REPO / "backend" / "app" / "services" / "robot_manager.py").read_text()
    assert "_FAULT_FLAGS" not in backend_src, (
        "백엔드에 _FAULT_FLAGS 가 생겼다 — wrapper 와 일치하는지 확인하는 테스트를 추가할 것"
    )
