"""부하 감시 임계의 저장/적용 — `safety_store` 와 같은 자리, 같은 이유.

`load.py` 는 **순수 로직**이라고 머리말이 못박고 있다. 파일을 읽고 쓰는 코드를
거기 넣으면 하드웨어 없이 부를 수 있다는 성질이 깨진다.

## ⚠ 왜 팔마다인가 — 바닥 필터와 다른 점

바닥 필터는 설치 전체에 하나다 (바닥은 하나니까). 부하 임계는 **팔마다** 둔다:
같은 모델이라도 슬립은 특정 개체의 특정 관절에서 반복해서 나기 때문이다
(실기에서 can3 의 joint5 가 그랬다). 하나로 묶으면 그 관절을 잡으려고 조인
임계가 멀쩡한 나머지 세 팔을 종일 울리게 만든다.

## ⚠ 임계는 아직 잠정이다 — 화면이 그렇게 말해야 한다

`load.PROVISIONAL_WARN_NM` 은 실기 기록(층 1)과 리셋 간극(층 2)을 맞대기 전까지
추정이고, 그 사실이 UI 까지 따라가야 한다 (`provisional`).

첫 실측이 이미 근거 하나를 뒤집었다 — 아래 `WARN_MAX_NM` 주석을 보라. 임계를
"어디서 끊기나" 로 정하려던 접근 자체가 흔들린 셈이라, 기본값 6.0 도 같은
운명일 수 있다. 그래서 기록을 계속 쌓는다.
"""

from __future__ import annotations

import json
import logging
from dataclasses import replace
from pathlib import Path

from piper_robot.arm import CONFIG_DIR
from piper_robot.kinematics import ARM_JOINTS
from piper_robot.load import EFFORT_COEFF, LoadLimits, amps_for

logger = logging.getLogger(__name__)

PATH = CONFIG_DIR / "load.json"

# 사람이 고칠 수 있는 값만 노출한다. `release`(히스테리시스 비율)는 UI 에 두지
# 않는다 — 1.0 으로 올리면 경보가 임계 언저리에서 초당 몇 번씩 깜빡이고, 그
# 소음은 경보를 아예 끈 것보다 나쁘다. 고칠 이유가 있으면 코드에서 고친다.
EDITABLE = ("enabled", "warn_nm", "dwell_s", "per_joint")

# 임계의 허용 범위 (N·m).
#
#  위쪽: ⚠ **처음 적은 근거가 틀렸다.** "드라이버가 10.3A(≈9.9N·m)에서 스스로
#        끊는다" 고 보고 상한을 9.0 으로 잘랐는데, 그 10.3A 는 `diagnostics` 의
#        **joint5** 스톨 한 건이었고 보편 한계가 아니었다. 2026-09-09 실측에서
#        can0 joint2 가 **11.4A / 13.5N·m** 까지 갔는데 드라이버는 안 끊었다.
#
#        게다가 전류→토크 계수가 관절군마다 다르므로(J1~3 ×1.18125, J4~6
#        ×0.95844) 같은 전류가 같은 토크가 아니다 — 한 숫자로 자르면 J1~3 은
#        정상 동작 구간을 못 덮는다. 9.0 이었다면 joint2 에는 쓸 만한 임계를
#        아예 못 넣었을 것이다.
#
#        그래서 상한은 **전류 기준**으로 잡고 가장 큰 계수로 환산한다. 이건
#        물리적 한계 주장이 아니라 말이 안 되는 값을 막는 난간이다.
#
#  아래쪽: 0.5 밑은 중력 보상 전류에도 걸린다. 팔이 가만히 있어도 울리는 경보는
#        하루면 아무도 안 읽는다.
WARN_MIN_NM = 0.5
#: 난간의 전류 기준 (A). 실측 최대(11.4A)보다 위에 둔다 — 관측한 값까지는
#: 임계로 고를 수 있어야 한다.
WARN_MAX_A = 12.5
WARN_MAX_NM = round(WARN_MAX_A * max(EFFORT_COEFF.values()), 1)

# 지속 판정의 허용 범위 (초). 0.05 = 표본 하나(20Hz)라 사실상 순간 피크까지
# 세는 값이고, 5.0 이면 웬만한 과부하가 다 끝난 뒤에 울린다.
DWELL_MIN_S = 0.05
DWELL_MAX_S = 5.0


def clamp_warn(v: float) -> float:
    return max(WARN_MIN_NM, min(WARN_MAX_NM, float(v)))


def clamp_dwell(v: float) -> float:
    return max(DWELL_MIN_S, min(DWELL_MAX_S, float(v)))


def load() -> dict[str, LoadLimits]:
    """팔별 저장된 임계. 없는 팔은 호출자가 기본값(`LoadLimits()`)을 쓴다."""
    try:
        raw = json.loads(PATH.read_text()).get("arms", {})
    except FileNotFoundError:
        return {}
    except Exception as exc:
        logger.warning("부하 임계를 읽지 못했습니다 (%s): %s — 기본값을 씁니다", PATH, exc)
        return {}
    out: dict[str, LoadLimits] = {}
    for iface, patch in (raw or {}).items():
        if isinstance(patch, dict):
            out[iface] = _apply(LoadLimits(), patch)
    return out


def save(all_limits: dict[str, LoadLimits]) -> None:
    PATH.parent.mkdir(parents=True, exist_ok=True)
    PATH.write_text(json.dumps({"arms": {
        iface: {"enabled": cfg.enabled, "warn_nm": cfg.warn_nm,
                "dwell_s": cfg.dwell_s, "per_joint": dict(cfg.per_joint)}
        for iface, cfg in all_limits.items()}}, indent=2))
    logger.info("부하 임계 저장: %s", ", ".join(sorted(all_limits)) or "(없음)")


def _apply(base: LoadLimits, patch: dict) -> LoadLimits:
    """`EDITABLE` 만 반영한다. 모르는 키는 조용히 버린다 — UI 가 보낸 오타가
    안전 파라미터를 바꾸면 안 된다."""
    out: dict = {}
    if "enabled" in patch:
        out["enabled"] = bool(patch["enabled"])
    for key, clamp in (("warn_nm", clamp_warn), ("dwell_s", clamp_dwell)):
        if key in patch:
            try:
                out[key] = clamp(patch[key])
            except (TypeError, ValueError):
                logger.warning("%s 값이 숫자가 아닙니다: %r — 무시", key, patch[key])
    if "per_joint" in patch:
        out["per_joint"] = _clean_per_joint(patch["per_joint"], base)
    return replace(base, **out) if out else base


def _clean_per_joint(raw, base: LoadLimits) -> dict[str, float]:
    """관절별 임계에서 **아는 관절의 숫자만** 남긴다.

    ⚠ `None`/빈 문자열은 "이 관절은 공통값을 쓴다" 는 뜻이라 지운다. 0 으로
      바꿔 저장하면 그 관절이 영구히 울린다 — 화면에서 입력을 지운 사람의
      의도와 정반대다.
    """
    out: dict[str, float] = {}
    if not isinstance(raw, dict):
        return out
    for joint, v in raw.items():
        if joint not in ARM_JOINTS or v is None or v == "":
            continue
        try:
            out[joint] = clamp_warn(v)
        except (TypeError, ValueError):
            logger.warning("%s 임계가 숫자가 아닙니다: %r — 무시", joint, v)
    return out


def as_dict(iface: str, cfg: LoadLimits) -> dict:
    """UI 용. **전류 환산을 함께 낸다** — 사람은 A 로 생각하는데 슬립은 토크로
    나므로, 둘을 같이 보여주지 않으면 숫자를 고를 근거가 없다."""
    return {
        "iface": iface,
        "enabled": cfg.enabled,
        "warn_nm": cfg.warn_nm,
        "dwell_s": cfg.dwell_s,
        "per_joint": dict(cfg.per_joint),
        # 관절별 실효 임계와 그 전류 환산 — 화면이 계산하지 않게 여기서 낸다.
        "effective": {j: {"nm": round(cfg.warn_for(j), 3),
                          "a": round(amps_for(j, cfg.warn_for(j)), 3)}
                      for j in ARM_JOINTS},
        "range_nm": [WARN_MIN_NM, WARN_MAX_NM],
        "range_dwell_s": [DWELL_MIN_S, DWELL_MAX_S],
        "default_nm": LoadLimits().warn_nm,
        # ⚠ 화면이 이 숫자를 근거 있는 값처럼 보여주면 안 된다.
        "provisional": True,
    }
