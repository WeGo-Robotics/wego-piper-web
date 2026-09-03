"""컨트롤 값을 **순서대로** 밀어 넣는다 — 조용한 실패를 막는 유일한 방법.

## 왜 dict 순회로는 안 되는가

`auto_exposure` 가 자동이면 커널은 `exposure_time_absolute` 에 `inactive` 플래그를 세우고
쓰기를 **조용히 무시한다.** 에러도 안 난다. dict 를 그냥 돌면 순서가 운에 맡겨지고,
"적용했는데 안 먹었다"가 재현되지 않는 형태로 나온다. RealSense 의
`enable_auto_exposure` ↔ `exposure`/`gain` 도 같은 관계다.

그래서 쓰기를 4단계로 나눈다:

1. **스위치를 수동으로** — 종속 값을 쓸 항목만
2. **종속 값** — exposure, white balance temperature, focus, gain
3. **독립 값** — brightness, contrast, saturation …
4. **스위치를 프로파일이 원하는 값으로** — 자동을 원했으면 여기서 되돌린다

4단계가 자동으로 되돌리면 2단계에서 쓴 종속 값은 무시된다. 그건 **정상**이고
`locked` 로 분류한다 — 실패로 세면 사용자가 고칠 수 없는 경고를 계속 보게 된다.

## v4l2 `auto_exposure` 는 값이 반직관적이다

**1 = Manual Mode, 3 = Aperture Priority(자동)** 이다. "1이니까 자동" 으로 읽으면
정확히 거꾸로 동작한다. 그래서 수동값을 이름으로 추측하지 않고
`type`(2=bool, 3=menu) 과 아래 표로 판정한다.

## 여기는 장치를 모른다

`list_controls()` / `set_control()` 두 콜러블만 받는다. camerad(v4l2)와 rsd(RealSense)가
**같은 dict 모양**을 쓰기 때문에 가능한 일이고, 덕분에 이 로직이 한 벌만 존재한다.
`plan()` 은 순수 함수라 장치 없이 테스트된다.
"""

import logging
import time

logger = logging.getLogger(__name__)

# 스위치 → 그 스위치가 잠그는 종속 컨트롤들.
# v4l2 와 RealSense 이름이 섞여 있다 — 한쪽에 없는 이름은 그냥 안 맞을 뿐이라 무해하다.
AUTO_SWITCHES: dict[str, tuple[str, ...]] = {
    # v4l2 (uvcvideo)
    "auto_exposure": ("exposure_time_absolute", "exposure_absolute", "gain"),
    "exposure_auto": ("exposure_absolute", "exposure_time_absolute"),  # 구형 커널 이름
    "white_balance_automatic": ("white_balance_temperature",),
    "white_balance_temperature_auto": ("white_balance_temperature",),  # 구형
    "focus_automatic_continuous": ("focus_absolute",),
    "focus_auto": ("focus_absolute",),                                  # 구형
    # RealSense
    "enable_auto_exposure": ("exposure", "gain"),
    "enable_auto_white_balance": ("white_balance",),
}

# 종속 컨트롤 → 그것을 잠그는 스위치들 (역인덱스)
_LOCKED_BY: dict[str, set[str]] = {}
for _sw, _deps in AUTO_SWITCHES.items():
    for _d in _deps:
        _LOCKED_BY.setdefault(_d, set()).add(_sw)

# 컨트롤 표시 단위 — 화면이 값 옆에 붙인다. **규격·실측이 정한 사실만** 적는다:
# V4L2 의 exposure*_absolute 는 100µs 단위(V4L2_CID_EXPOSURE_ABSOLUTE 규격 —
# 333 이면 33.3ms), white balance 는 켈빈. 단위를 모르는 컨트롤에는 안 붙인다 —
# 틀린 단위는 없는 단위보다 나쁘다.
#
# ⚠ RealSense 의 `exposure` 는 여기 없다 — **센서마다 단위가 달라** 이름만으로
#   못 정한다. `unit_for()` 가 범위를 보고 가른다.
CONTROL_UNITS: dict[str, str] = {
    "exposure_time_absolute": "×100µs",
    "exposure_absolute": "×100µs",       # 구형 커널 이름
    "white_balance_temperature": "K",
    "white_balance": "K",                # RealSense
}


def exposure_unit_scale(max_value) -> int:
    """RealSense `exposure` 를 µs 로 환산할 때 곱하는 배율.

    같은 이름인데 **센서마다 단위가 다르다** (실측 2026-09-02):

    - D415/D435/D455 의 RGB 센서: max 10000, **100µs 단위** (값 266 = 26.6ms.
      µs 로 읽으면 최대 노출이 10ms 라는 뜻이 되는데, 그런 카메라는 없다)
    - 스테레오 모듈 — 깊이, 그리고 **RGB 센서가 없는 D405 의 컬러**: max 165000, **µs**

    모델명 목록 대신 **범위로 가른다.** 장치가 스스로 신고하는 값이라 새 모델에도
    맞을 가능성이 높고, 틀려도 range 를 보라는 단서가 남는다. 경계 2만은 두 세계
    (1만 / 16.5만) 사이 어디든 상관없는 값이다.
    """
    if max_value is None:
        return 1
    try:
        return 100 if float(max_value) <= 20000 else 1
    except (TypeError, ValueError):
        return 1


EXPOSURE_NAMES: tuple[str, ...] = ("exposure_time_absolute", "exposure_absolute",
                                   "exposure")


def exposure_us(ctrl: dict) -> float | None:
    """노출 컨트롤 하나 → µs. 노출이 아니거나 값이 없으면 None.

    ⚠ 환산은 **여기 한 곳**이다. 세 이름이 서로 다른 단위를 쓴다 — v4l2 의 둘은
    ×100µs 로 고정이고, RealSense `exposure` 는 범위가 단위의 지문이다
    (`exposure_unit_scale`). 호출부마다 다시 적으면 언젠가 100배 틀린 노출이
    화면에 뜨는데, 그건 눈으로 못 거른다.
    """
    name = ctrl.get("name", "")
    if name not in EXPOSURE_NAMES:
        return None
    v = ctrl.get("value")
    if v is None:
        return None
    scale = exposure_unit_scale(ctrl.get("max")) if name == "exposure" else 100
    try:
        return float(v) * scale
    except (TypeError, ValueError):
        return None


def unit_for(ctrl: dict) -> str | None:
    """컨트롤 하나의 표시 단위. 모르면 None — 지어내지 않는다."""
    name = ctrl.get("name", "")
    if name == "exposure":               # RealSense — 범위가 단위의 지문이다
        return "×100µs" if exposure_unit_scale(ctrl.get("max")) == 100 else "µs"
    return CONTROL_UNITS.get(name)

# 종속 컨트롤 전체 — capture 가 "default 와 같아도 저장" 판정에 쓴다.
# 스위치만 저장하고 값을 빼면 프로파일 **전환**이 완결되지 않는다: 야간(노출 2000)
# 적용 뒤 주간(노출=default 라 미저장)을 적용하면 2000 이 잔류한다 (camera-profiles.md G2).
DEPENDENT_CONTROLS: frozenset = frozenset(_LOCKED_BY)

# v4l2 menu 스위치의 "수동" 값. bool(type 2)은 0 이 수동이라 표가 필요 없다.
_MENU_MANUAL = {
    "auto_exposure": 1,      # V4L2_EXPOSURE_MANUAL
    "exposure_auto": 1,      # 구형 이름도 1 = Manual
}

# 쓰기 결과 분류
OK = "ok"
LOCKED = "locked"    # 자동 모드가 잠갔다 — 실패가 아니다
FAILED = "failed"
SKIPPED = "skipped"  # 장치에 없거나 readonly


def manual_switch_value(name: str) -> int:
    """장치 없이 **프로파일 값만 보고** 스위치의 수동 값을 판정할 때 쓴다.

    menu 스위치(auto_exposure 계열)는 1 = Manual, bool 스위치는 0 이 수동이다.
    장치가 있으면 `manual_value(ctrl)` 를 쓴다 — 그쪽은 범위 검증까지 한다.
    """
    return _MENU_MANUAL.get(name, 0)


def manual_value(ctrl: dict) -> int | None:
    """이 스위치를 **수동**으로 돌리는 값. 모르면 `None` (건드리지 않는다)."""
    name = ctrl.get("name", "")
    ctype = ctrl.get("type")
    if ctype == 2:                      # bool — 0 이 수동
        return 0
    if name in _MENU_MANUAL:            # menu — 이름을 알아야 한다
        val = _MENU_MANUAL[name]
        lo, hi = ctrl.get("min", val), ctrl.get("max", val)
        return val if lo <= val <= hi else None
    return None


def plan(controls: list[dict], wanted: dict) -> list[tuple[str, int]]:
    """쓸 순서를 정한다. **순수 함수** — 장치를 건드리지 않는다.

    `controls` 는 `list_controls()` 결과, `wanted` 는 `{이름: 값}`.
    장치에 없거나 readonly 인 항목은 계획에서 빠진다(호출부가 `skipped` 로 기록한다).
    같은 스위치가 1단계와 4단계에 두 번 나올 수 있다 — 의도한 것이다.
    """
    by_name = {c["name"]: c for c in controls}
    writable = {n: c for n, c in by_name.items() if not c.get("readonly")}

    # `wanted` 중 실제로 쓸 수 있는 것만
    todo = {n: v for n, v in wanted.items() if n in writable}

    switches = [n for n in todo if n in AUTO_SWITCHES]
    dependents = [n for n in todo if n in _LOCKED_BY]
    independents = [n for n in todo if n not in AUTO_SWITCHES and n not in _LOCKED_BY]

    out: list[tuple[str, float]] = []

    # 1단계 — 종속 값을 쓸 예정인 스위치만 수동으로. 쓸 종속 값이 없으면
    # 스위치를 건드릴 이유가 없다(자동으로 잘 돌던 카메라를 흔들지 않는다).
    need_manual: set[str] = set()
    for dep in dependents:
        need_manual |= _LOCKED_BY[dep] & writable.keys()
    for name in sorted(need_manual):
        mv = manual_value(writable[name])
        if mv is not None:
            out.append((name, mv))

    # 2단계 — 종속 값
    for name in sorted(dependents):
        out.append((name, todo[name]))

    # 3단계 — 독립 값
    for name in sorted(independents):
        out.append((name, todo[name]))

    # 4단계 — 스위치를 프로파일이 원하는 값으로. 자동을 원했으면 여기서 되돌아간다.
    for name in sorted(switches):
        out.append((name, todo[name]))

    return out


def _same(got, want) -> bool:
    """값이 같은가. **정수로 비교하면 안 된다.**

    RealSense 의 `depth_units` 는 1e-4 같은 실수다. `int()` 를 씌우면 0 이 되고,
    그 0 을 다시 밀어 넣으면 깊이 스케일이 0 이 된다 — 실기에서 실제로 걸렸다.
    장치가 값을 근사해서 돌려주는 경우도 있어(레지스터 해상도) 상대 오차로 본다.
    """
    try:
        g, w = float(got), float(want)
    except (TypeError, ValueError):
        return False
    return abs(g - w) <= max(1e-6, abs(w) * 1e-4)


def _classify(ctrl: dict | None, want) -> str:
    """read-back 결과 판정. 값이 다르면 **왜** 다른지까지 말해야 한다."""
    if ctrl is None:
        return SKIPPED
    # ⚠ **값보다 플래그를 먼저 본다.** 1단계에서 수동으로 돌려 값을 넣은 뒤 4단계에서
    # 자동으로 되돌리면, 레지스터에는 넣은 값이 남아 있지만 **실제로 노출을 정하는 건
    # 자동 로직**이다. 값만 보고 `ok` 라 하면 "설정대로 돈다"는 거짓말이 된다.
    # 자동 모드·readonly 가 잠근 것은 실패가 아니다 — 사용자가 고칠 게 없다.
    if ctrl.get("inactive") or ctrl.get("readonly"):
        return LOCKED
    return OK if _same(ctrl.get("value"), want) else FAILED


def apply_controls(
    list_controls,
    set_control,
    wanted: dict,
    *,
    budget_s: float = 2.0,
    label: str = "",
) -> dict:
    """계획대로 쓰고 다시 읽어 검증한다. **예외를 올리지 않는다.**

    프로파일 적용이 실패해서 카메라 연결이 실패하는 일은 없어야 한다 —
    조명이 틀린 영상이 안 나오는 영상보다 낫다.

    `budget_s` 를 넘기면 남은 쓰기를 건너뛴다. 컨트롤 하나가 몇 초씩 무는 장치가
    있고(D405 UVC 질의), 그게 연결 전체를 붙잡으면 안 된다.
    """
    started = time.monotonic()
    result = {"applied": 0, "locked": 0, "failed": 0, "skipped": 0,
              "details": [], "truncated": False}
    if not wanted:
        return result

    try:
        controls = list_controls()
    except Exception as exc:
        logger.warning("컨트롤 목록을 읽지 못했다 (%s): %s", label, exc)
        result["skipped"] = len(wanted)
        return result

    by_name = {c["name"]: c for c in controls}
    writes = plan(controls, wanted)

    # 계획에 아예 못 들어간 것 — 장치에 없거나 readonly
    planned = {n for n, _ in writes}
    for name in wanted:
        if name not in planned:
            result["skipped"] += 1
            result["details"].append({
                "name": name, "want": wanted[name], "got": None,
                "status": SKIPPED,
                "reason": "장치에 없음" if name not in by_name else "readonly",
            })

    for name, value in writes:
        if time.monotonic() - started > budget_s:
            result["truncated"] = True
            logger.warning("컨트롤 적용 예산(%.1fs) 초과 — 남은 항목 생략 (%s)",
                           budget_s, label)
            break
        try:
            set_control(name, value)
        except Exception as exc:
            logger.warning("컨트롤 쓰기 실패 %s=%s (%s): %s", name, value, label, exc)

    # read-back — 쓴 값이 아니라 **프로파일이 원한 값**과 비교한다.
    # 1단계에서 임시로 수동으로 돌린 스위치는 4단계에서 원래 값으로 돌아갔어야 한다.
    try:
        after = {c["name"]: c for c in list_controls()}
    except Exception as exc:
        logger.warning("read-back 실패 (%s): %s", label, exc)
        after = {}

    for name, want in wanted.items():
        if name not in planned:
            continue  # 위에서 skipped 로 기록했다
        status = _classify(after.get(name), want)
        result["details"].append({
            "name": name, "want": want,
            "got": after[name]["value"] if name in after else None,
            "status": status,
        })
        result[{OK: "applied", LOCKED: "locked",
                FAILED: "failed", SKIPPED: "skipped"}[status]] += 1

    result["details"].sort(key=lambda d: d["name"])
    return result
