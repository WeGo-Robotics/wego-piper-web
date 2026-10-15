"""카메라 스트림 이름 — 규칙 · 기본 이름 · 유일성 · 디렉토리 (feature/stream-based-cameras.md).

등록한 카메라는 **사용자가 붙이는 이름**으로 불린다. 별칭(`label`)이 이 이름으로 합쳐졌다.

## 왜 규칙을 한 곳에 두나

이름은 세그먼트 파일 이름(`/dev/shm/piper.cam.<이름>`)이자 CLI 인자·URL·JSON 값이다.
허용 문자가 화면 검증과 서버 검증에서 갈리면 "화면은 통과했는데 서버가 거절"이 되고,
서버가 느슨하면 CLI 인자에 공백이 새어 들어간다. 판정은 **여기 하나**이고 화면은
사유를 받아 보여줄 뿐이다.

## 이 모듈은 `CameraInfo` 를 import 하지 않는다

`camera_manager` 가 이 모듈을 쓴다. 거꾸로 import 하면 순환이므로, 카메라는
`id`·`stream`·`aliases` 속성을 가진 아무 객체(덕 타이핑)로만 다룬다.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping

# 영문자·숫자·`_`·`-`. **첫 글자와 끝 글자는 영숫자.** 길이 1~40.
# 끝 글자를 막는 이유: 세그먼트 규칙 함수(`piper_shm.segment_for_camera`)가 앞뒤 `_` 를
# 떼므로, `top_` 은 그 함수를 한 번 지나면 `top` 이 된다 — 이름이 바뀌어 다른 세그먼트를 찾는다.
NAME_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9_-]{0,38}[A-Za-z0-9])?$")
MAX_LEN = 40
# 장치 이름이 비영문이라 슬러그가 비었을 때의 바탕.
FALLBACK_BASE = "cam"
# 개명 이력(alias) 상한. 오래된 것부터 버린다.
MAX_ALIASES = 8


class StreamNameError(ValueError):
    """이름이 규칙에 안 맞는다. 라우터가 400 으로 바꾼다. 메시지는 사람이 읽는 사유다."""


class StreamNameConflict(StreamNameError):
    """이미 다른 카메라가 쓰는 이름이다. 라우터가 409 로 바꾼다."""


# ── 이름 규칙 ─────────────────────────────────────────────────────────────

def validate(raw: object) -> str:
    """사용자가 준 이름을 다듬어 돌려준다. 규칙에 안 맞으면 `StreamNameError`.

    **조용히 고쳐 쓰지 않는다.** `탑뷰` 를 `cam_1` 로 바꿔 받아주면 사람은 자기가 정한
    이름이 적용된 줄 안다 — 안 맞으면 사유와 함께 돌려준다.
    """
    name = str(raw or "").strip()
    if not name:
        raise StreamNameError("이름이 비어 있습니다")
    n = len(name)
    if n > MAX_LEN:
        raise StreamNameError(f"이름은 {MAX_LEN}자까지입니다 (지금 {n}자)")
    if not NAME_RE.match(name):
        raise StreamNameError(
            "이름에는 영문자·숫자·_·- 만 쓸 수 있고 첫 글자와 끝 글자는 영문자나 숫자여야 합니다 "
            f"(공백·한글·기호 불가): {name!r}"
        )
    return name


def slug(device_name: str) -> str:
    """장치 이름 → 기본 이름의 바탕. `D435 Color` → `D435_Color`.

    비영문·기호는 `_` 하나로 줄이고 앞뒤 `_` 를 뗀다. 비면(장치 이름이 한글뿐 등)
    `cam`. 번호(`_12`)가 붙을 자리를 남겨 길이를 자른다.
    """
    s = re.sub(r"[^A-Za-z0-9]+", "_", str(device_name or "")).strip("_")
    return s[: MAX_LEN - 4].rstrip("_") or FALLBACK_BASE


def default_name(device_name: str, taken: Iterable[str]) -> str:
    """`슬러그_N` — N 은 `taken`(대소문자 무시)에 없는 가장 작은 번호. 한 대여도 `_1`."""
    used = {t.lower() for t in taken}
    base = slug(device_name)
    n = 1
    while f"{base}_{n}".lower() in used:
        n += 1
    return f"{base}_{n}"


def migrate_label(label: object) -> str | None:
    """옛 별칭 → 이름으로 쓸 수 있으면 그 이름, 아니면 `None`.

    공백은 `_` 로 바꿔 준다(`Top View` → `Top_View`). 그 밖의 기호·한글은 못 쓴다.
    못 쓴다고 **이 함수가 지우는 것이 아니다** — 부르는 쪽이 기본 이름을 주고 사용자에게
    알린다.
    """
    s = re.sub(r"\s+", "_", str(label or "").strip())
    try:
        return validate(s)
    except StreamNameError:
        return None


# ── 유일성 · 개명 이력 ────────────────────────────────────────────────────

def names_in_use(cameras: Iterable, *, except_id: str | None = None) -> set[str]:
    """지금 쓰이는 **현재 이름**(소문자). `except_id` 의 카메라는 뺀다(자기 이름은 충돌 아님)."""
    return {
        c.stream.lower() for c in cameras
        if c.stream and c.id != except_id
    }


def rule_names(cameras: Iterable, *, except_id: str | None = None) -> set[str]:
    """다른 카메라들의 **규칙 이름**(`dev_video0`·`rs_<시리얼>_color`, 소문자).

    이름을 지정하지 않은 카메라(옛 데몬이 연결한 것 등)는 아직 이 이름으로 발행된다. 사용자가
    다른 카메라에 같은 이름을 붙이면 **한 세그먼트에 라이터가 둘**이 된다 — 그 이름은 막는다.
    """
    from piper_shm import rule_segment_name

    return {rule_segment_name(c.id).lower() for c in cameras if c.id != except_id}


def reserved_names(cameras: Iterable) -> set[str]:
    """기본 번호 매기기가 **피해야 하는** 이름 — 현재 이름 + 모든 개명 이력 + 규칙 이름.

    옛 이름이 다른 카메라에 재사용되면 그 이름으로 저장된 매핑이 엉뚱한 카메라로
    해석된다. 새 기본 이름은 이력도 건너뛴다.
    """
    cameras = list(cameras)
    out: set[str] = rule_names(cameras)
    for c in cameras:
        if c.stream:
            out.add(c.stream.lower())
        out.update(a.lower() for a in c.aliases)
    return out


def check_unique(name: str, cameras: Iterable, *, except_id: str | None = None) -> None:
    """다른 카메라의 현재 이름과 겹치면 `StreamNameConflict`.

    **현재 이름이 이력보다 우선한다** — 다른 카메라의 *옛* 이름과 겹치는 것은 허용한다
    (카메라 둘의 역할을 맞바꿀 때 쓰는 이름이다). 그 이력은 `adopt` 가 지운다.
    """
    cameras = list(cameras)
    low = name.lower()
    if low in names_in_use(cameras, except_id=except_id) or low in rule_names(cameras, except_id=except_id):
        raise StreamNameConflict(f"'{name}' 은(는) 다른 카메라가 쓰고 있는 이름입니다")


def adopt(cam, new_name: str, others: Iterable) -> None:
    """`cam` 의 이름을 `new_name` 으로 바꾸고 이력을 정리한다. 검증은 부르는 쪽이 끝냈다.

    - 옛 이름은 `cam.aliases` 로 간다(같은 이름이면 안 쌓는다).
    - 새 이름이 자기 이력에 있었으면 거기서 뺀다.
    - 새 이름이 **다른 카메라의 이력**에 있었으면 그쪽에서 뺀다 — 현재 이름이 이긴다.
    """
    low = new_name.lower()
    old = cam.stream
    cam.aliases = [a for a in cam.aliases if a.lower() != low]
    if old and old.lower() != low and old.lower() not in {a.lower() for a in cam.aliases}:
        cam.aliases.append(old)
    del cam.aliases[:-MAX_ALIASES]
    cam.stream = new_name
    for other in others:
        if other is not cam:
            other.aliases = [a for a in other.aliases if a.lower() != low]


# ── 조회 ──────────────────────────────────────────────────────────────────

def segment_of(cam) -> str:
    """이 카메라를 소비자가 열 **세그먼트 이름**.

    ⚠ **이 함수가 이음매다.** 지금은 데몬이 장치 id 에서 규칙으로 이름을 만들어 발행하므로
    같은 규칙을 쓴다. 데몬이 이름을 받아 발행하게 되면(feature/stream-based-cameras.md §5-4)
    이 줄만 `cam.stream` 으로 바뀐다 — 소비자 코드는 안 바뀐다.
    """
    from piper_shm import segment_for_camera

    return segment_for_camera(cam.id)


def resolve(value: str, cameras: Mapping[str, object]):
    """스트림 이름 · 개명 이력 · 옛 장치 id · 세그먼트 이름 → 카메라(없으면 `None`).

    순서가 의미다 — **장치 id → 현재 이름 → 이력 → 세그먼트 이름.** 이름 규칙이 `/`·`:` 를
    막으므로 장치 id 와 이름이 겹칠 수 없다. 이력은 현재 이름이 모두 안 맞았을 때만 본다.
    """
    if not value:
        return None
    cam = cameras.get(value)
    if cam is not None:
        return cam
    low = value.lower()
    cams = list(cameras.values())
    for c in cams:
        if c.stream and c.stream.lower() == low:
            return c
    for c in cams:
        if any(a.lower() == low for a in c.aliases):
            return c
    for c in cams:
        if segment_of(c) == value:
            return c
    return None


def describe(cam, *, width: int | None = None, height: int | None = None,
             fps: int | None = None, streaming: bool | None = None) -> dict:
    """디렉토리 한 줄. 선택기가 이걸로 "보이되 비활성"을 판단한다.

    상태는 **등록된 카메라**에 대해서만 말한다(미등록은 스트림이 아니다).

        live     연결돼 있다 → 고를 수 있다
        idle     꽂혀 있는데 연결 안 됨 → 보이되 비활성 (`연결` 로 복귀)
        missing  보이지 않는다(USB 빠짐) → 보이되 비활성

    ⚠ `stalled`(연결은 됐는데 프레임이 안 옴)를 비활성으로 두지 않았다. 사용자 규칙이
    "연결된 것은 선택할 수 있다" 이고, 신선도 판정(`streaming`)은 순간적으로 틀릴 수 있어
    — 막으면 멀쩡한 카메라로 수집을 못 시작한다. 대신 `streaming` 을 같이 실어 화면이
    경고만 달게 한다.
    """
    if not cam.present:
        state, reason = "missing", "카메라가 보이지 않습니다 (USB 빠짐)"
    elif not cam.connected:
        state, reason = "idle", "연결 안 됨"
    else:
        state, reason = "live", ""
    return {
        "stream": cam.stream,
        "segment": segment_of(cam),
        "device_id": cam.id,
        "device_name": cam.name,
        "cam_type": cam.cam_type,
        # 종류. v4l2 는 stream_type 이 비어 있고 항상 컬러다.
        "kind": cam.stream_type or "color",
        "state": state,
        "reason": reason,
        "selectable": state == "live",
        # 프레임이 **지금도** 오는가. `None` 은 모른다(연결 안 됨). 막는 근거가 아니라
        # 화면이 경고를 다는 근거다.
        "streaming": streaming,
        "aliases": list(cam.aliases),
        "width": width,
        "height": height,
        "fps": fps,
    }
