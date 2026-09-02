"""카메라 프로파일 — 노출·화이트밸런스 같은 **컨트롤 값을 디스크에 남긴다.**

## 이것만 남은 이유

`feature/camera-profiles.md` 는 "설정이 초기화된다"의 원인을 7개로 나눴는데,
데몬 분리(3b-5) 이후 넷이 사라졌다. 해상도·FPS 는 이제 요청이 데몬까지 가고,
장치를 놓는 단계가 없어졌고, 여는 주체가 하나뿐이라 트리거를 배선할 곳도 하나다.

**남은 것은 이 파일이 하는 일 하나뿐이었다**: 컨트롤 값이 어디에도 저장되지 않아
서버 재시작·USB 재열거·하드웨어 리셋 중 아무거나 한 번이면 전부 날아가는 것.

## 저장은 공통 프리셋 스토어에

`presets.py`(domain=`camera`, scope=`device`)를 쓴다. 로봇·학습·추론이 이미 거기 있고,
전용 스토어를 또 만들면 [parameter-presets](../../../feature/parameter-presets.md)가
말하는 일곱 번째 사본이 된다. 카메라는 이 기계에 물린 물건이라 `device` 다.

## 적용은 여기가 아니라 데몬이 한다

여기는 **값과 매칭**만 안다. 순서 있는 쓰기와 read-back 검증은
`piper_cam.controls` 가 하고 camerad/rsd 가 부른다 — 장치를 여는 그 자리에서.
"""

import json
import logging

from app.core.config import settings
from app.services import presets

logger = logging.getLogger(__name__)

DOMAIN = "camera"

# 활성 프로파일 이름. 프리셋 스토어는 "무엇이 활성인가"를 모르므로 여기 따로 둔다.
ACTIVE_PATH = settings.config_dir / "camera_active_profile.json"

# 저장 대상에서 빼는 컨트롤. 값이 있어도 프로파일에 넣지 않는다.
#  - readonly: 쓸 수 없다
#  - 스냅샷 성격: 다시 밀어 넣는 게 의미 없거나 해로운 것
SKIP_CONTROLS = {
    "frames_queue_size",       # RealSense 내부 큐 — 조명과 무관
    # ⚠ **깊이 스케일은 데이터셋 계약이다.** 픽셀값의 뜻을 정하는 값이라
    # 조명 프로파일이 건드릴 물건이 아니다. 깊이 인코딩은 rsd 가 소유하고
    # `meta/piper_cameras.json` 사이드카에 따로 기록된다.
    "depth_units",
}


def _num(value):
    """정수는 정수로, 실수는 실수로. **무조건 `int()` 하면 안 된다.**

    `depth_units` 는 1e-4 같은 값이라 `int()` 를 씌우면 0 이 된다 — 실기에서
    실제로 0 으로 저장됐다. 지금은 그 항목을 아예 빼지만, 다른 실수 옵션이
    언제든 생길 수 있으므로 변환 자체를 고친다.
    """
    f = float(value)
    return int(f) if f.is_integer() else f


def _entry_for(cam) -> dict:
    """카메라 한 대의 매칭 정보. 값(`controls`)은 호출부가 채운다."""
    return {
        "key": cam.profile_key,
        "match": {
            "cam_type": cam.cam_type,
            "usb_port": cam.usb_port,
            "name": cam.name,
            "serial": cam.serial,
            "stream_type": cam.stream_type,
            "last_dev": cam.id,
        },
        "stream": {
            "width": cam.width, "height": cam.height, "fps": cam.fps,
            "fourcc": cam.fourcc,
        },
        "controls": {},
    }


def capture(cameras: list) -> dict:
    """지금 장치에 들어 있는 값을 읽어 프로파일 `values` 를 만든다.

    ⚠ **`min/max/step/default` 는 저장하지 않는다.** 그건 장치가 진실이고
    펌웨어가 바뀌면 같이 바뀐다. 저장해두면 옛 범위로 클램프하게 된다.

    default 와 같은 값도 뺀다 — 파일이 짧아지고 펌웨어 차이에 강해진다.
    단 두 부류는 **default 와 같아도 항상 저장한다**:

    - **자동 스위치** — 적용 순서를 정하는 축이라 빠지면 종속 값이 조용히
      무시되는 그 상태로 돌아간다
    - **그 종속 값**(노출·WB·gain·초점) — 빠지면 프로파일 **전환**이 완결되지
      않는다: 야간(노출 2000) 적용 뒤 주간(노출=default 라 미저장)을 적용하면
      스위치만 수동으로 돌아오고 **2000 이 잔류**한다 (문서 검토 G2)
    """
    from piper_cam.controls import AUTO_SWITCHES, DEPENDENT_CONTROLS

    out = []
    for cam in cameras:
        entry = _entry_for(cam)
        for ctrl in cam.get_controls() or []:
            name = ctrl.get("name", "")
            if not name or name in SKIP_CONTROLS or ctrl.get("readonly"):
                continue
            value = ctrl.get("value")
            if value is None:
                continue
            if (name not in AUTO_SWITCHES and name not in DEPENDENT_CONTROLS
                    and value == ctrl.get("default")):
                continue
            entry["controls"][name] = _num(value)
        out.append(entry)
    return {"cameras": out}


def match(entries: list[dict], cameras: list) -> dict[str, dict]:
    """프로파일 항목 ↔ 지금 보이는 카메라. 반환은 `{cam_id: entry}`.

    키가 최우선이고, 못 맞추면 `last_dev` → 이름 순으로 내려간다.
    **이름 폴백은 후보가 하나일 때만** — 같은 모델 두 대에 엉뚱하게 붙느니
    안 붙는 게 낫다.
    """
    by_key = {c.profile_key: c for c in cameras}
    by_id = {c.id: c for c in cameras}
    out: dict[str, dict] = {}
    used: set[str] = set()

    for entry in entries:
        m = entry.get("match") or {}
        cam = by_key.get(entry.get("key", ""))
        if cam is None:
            cam = by_id.get(m.get("last_dev", ""))
        if cam is None:
            name = m.get("name", "")
            hits = [c for c in cameras if c.name == name and c.id not in used]
            cam = hits[0] if len(hits) == 1 else None
        if cam is None or cam.id in used:
            continue
        used.add(cam.id)
        out[cam.id] = entry
    return out


# ── 활성 프로파일 ──

def active_name() -> str:
    try:
        return str(json.loads(ACTIVE_PATH.read_text()).get("name") or "")
    except Exception:
        return ""


def set_active(name: str) -> None:
    """`""` 이면 자동 적용을 끈다."""
    ACTIVE_PATH.parent.mkdir(parents=True, exist_ok=True)
    ACTIVE_PATH.write_text(json.dumps({"name": name}, ensure_ascii=False))
    logger.info("활성 카메라 프로파일: %s", name or "(없음)")


def active_entries() -> list[dict]:
    name = active_name()
    if not name:
        return []
    try:
        preset = presets.get(DOMAIN, name)
    except presets.PresetError:
        return []
    if preset is None:
        logger.warning("활성 프로파일 %r 이 없습니다 — 자동 적용을 건너뜁니다", name)
        return []
    return list((preset.values or {}).get("cameras") or [])


def controls_for(cam) -> dict:
    """이 카메라에 적용할 컨트롤 값. **연결 경로가 부르는 유일한 함수다.**

    카메라 한 대만 보므로 이름 폴백의 "후보가 하나일 때만" 규칙을 쓸 수 없다 —
    그래서 여기서는 **키와 `last_dev` 만** 본다. 이름으로 넘겨짚어 엉뚱한
    카메라에 남의 노출값을 밀어 넣는 것보다, 적용 안 되는 편이 낫다.
    """
    key = cam.profile_key
    for entry in active_entries():
        m = entry.get("match") or {}
        if entry.get("key") == key or m.get("last_dev") == cam.id:
            return dict(entry.get("controls") or {})
    return {}


def apply(cameras: list, name: str = "") -> dict:
    """프로파일을 지금 연결된 카메라들에 밀어 넣는다 (수동 적용).

    연결 시 자동 적용과 **같은 데몬 함수**를 탄다 — 두 경로가 갈리면
    "수동으로는 되는데 자동으로는 안 된다"가 생긴다.
    """
    name = name or active_name()
    if not name:
        return {"profile": "", "cameras": [], "error": "활성 프로파일이 없습니다"}
    preset = presets.get(DOMAIN, name)
    if preset is None:
        return {"profile": name, "cameras": [], "error": "프로파일을 찾을 수 없습니다"}

    entries = list((preset.values or {}).get("cameras") or [])
    paired = match(entries, cameras)
    by_id = {c.id: c for c in cameras}

    results = []
    for cam_id, entry in paired.items():
        cam = by_id[cam_id]
        wanted = dict(entry.get("controls") or {})
        report = cam.apply_controls(wanted) if wanted else {}
        results.append({"key": entry.get("key", ""), "cam_id": cam_id,
                        "display_name": cam.label or cam.name, **report})

    unmatched = [e.get("key", "") for e in entries if e.get("key") not in
                 {r["key"] for r in results}]
    return {"profile": name, "cameras": results, "unmatched": unmatched}


# 재현성 검토가 보는 컨트롤 계열. 스위치 이름은 controls.AUTO_SWITCHES 의 부분집합이다 —
# 초점은 고정초점 카메라가 흔해서 **스위치가 있을 때만** 본다.
_FAMILIES: tuple[tuple[str, tuple[str, ...], bool], ...] = (
    ("노출", ("auto_exposure", "exposure_auto", "enable_auto_exposure"), True),
    ("화이트밸런스", ("white_balance_automatic", "white_balance_temperature_auto",
                     "enable_auto_white_balance"), True),
    ("초점", ("focus_automatic_continuous", "focus_auto"), False),
)


def validate(entries: list[dict]) -> list[dict]:
    """프로파일이 **재현 가능한가** — 경고 목록 (문서 검토 G1·G3). 순수 함수.

    프로파일의 존재 이유는 재현(노출·색이 다음 주에도 같게)인데, capture 는
    장치 상태를 충실히 기록할 뿐 그 상태가 재현 가능한지는 모른다. 여기서 따진다:

    - 자동인 채 저장 → 조명이 바뀌면 카메라가 따라간다 (G1)
    - 수동인데 종속 값 없음 → 장치에 남아 있던 값이 그대로 쓰인다
    - match 부재 → 어느 장치에도 적용되지 않는다

    **막지 않고 말만 한다** — 자동 프로파일도 일부러 쓸 수 있다(프리뷰용).
    반환: ``[{key, text}]``. 깊이 스트림 항목은 노출·WB 검토가 무의미해 건너뛴다.
    """
    from piper_cam.controls import AUTO_SWITCHES, manual_switch_value

    out: list[dict] = []

    def warn(key: str, text: str) -> None:
        out.append({"key": key, "text": text})

    for e in entries or []:
        key = str(e.get("key") or "(키 없음)")
        m = e.get("match") or {}
        controls = e.get("controls") or {}
        if not e.get("key") and not m.get("last_dev"):
            warn(key, "match 정보가 없어 어느 장치에도 적용되지 않습니다 — 재캡처하세요")
        if m.get("stream_type") == "depth":
            continue
        for fam, switches, required in _FAMILIES:
            present = [s for s in switches if s in controls]
            if not present:
                if required:
                    warn(key, f"{fam} 자동/수동 상태가 저장되지 않았습니다 — "
                              "장치의 그때그때 상태를 따르게 됩니다")
                continue
            sw = present[0]
            try:
                is_manual = float(controls[sw]) == float(manual_switch_value(sw))
            except (TypeError, ValueError):
                is_manual = False
            if not is_manual:
                warn(key, f"{fam}이 자동인 채 저장되어 있습니다 — 조명이 바뀌면 "
                          "카메라가 따라가 재현되지 않습니다")
                continue
            if not any(d in controls for d in AUTO_SWITCHES.get(sw, ())):
                warn(key, f"{fam}이 수동인데 값이 없습니다 — 장치에 남아 있던 "
                          "값이 그대로 쓰입니다")
    return out


def apply_for_task(name: str) -> dict:
    """작업(녹화·추론) 시작 직전의 **1회 적용**.

    `prepare_cameras` 가 장치를 연결한 **뒤에** 불러야 값이 장치에 닿는다.
    연결 시 자동 적용(활성 프로파일)과 별개로 작업이 지정한 프로파일을 한 번
    더 밀어 넣는다 — **활성은 바꾸지 않는다.** "이 작업은 이 기준"이라고 한
    것이지 기계의 기본을 바꾼 것이 아니다. 작업 도중 카메라가 재연결되면
    활성 프로파일이 다시 붙는데, 그 어긋남은 조명 감시(lighting-watch)가
    밝기·색 급변으로 잡는다.

    프로파일이 없으면 `error` 를 채워 돌려준다 — 호출부가 시작을 막는다.
    지정한 기준 없이 찍힌 에피소드가 조용히 섞이는 것보다 낫다.

    `warnings` 에는 **막지는 않지만 알아야 하는 것**을 담는다 (문서 검토 3번):
    프로파일 항목이 장치를 못 찾은 것(unmatched)과, 반대로 연결된 카메라 중
    프로파일이 안 덮는 것(uncovered). 문구는 여기서 만든다 — 화면이 조립하면
    한쪽만 고쳐져 어긋난다 (device_watch 와 같은 규칙).
    """
    from app.services.camera_manager import camera_manager

    cams = [c for c in camera_manager.cameras.values() if c.connected]
    report = apply(cams, name)
    if report.get("error"):
        return report

    warnings: list[str] = []
    if report.get("unmatched"):
        warnings.append(
            f"프로파일 '{name}' 의 항목 {len(report['unmatched'])}개가 지금 장치와 "
            f"안 맞습니다: {', '.join(report['unmatched'])} — 다른 장비 구성에서 "
            "캡처한 프로파일일 수 있습니다")
    matched = {r["cam_id"] for r in report.get("cameras", [])}
    uncovered = [(c.label or c.name) for c in cams if c.id not in matched]
    if uncovered:
        warnings.append(
            f"프로파일 '{name}' 이 안 덮는 카메라: {', '.join(uncovered)} — "
            "이 카메라는 노출·색 기준 없이 돕니다")
    report["warnings"] = warnings
    return report


def report(cameras: list) -> dict:
    """마지막 적용 결과 — 데몬이 들고 있는 것을 모아 온다."""
    out = []
    for cam in cameras:
        r = cam.last_apply_report()
        if r:
            out.append({"cam_id": cam.id, "display_name": cam.label or cam.name, **r})
    return {"profile": active_name(), "cameras": out}
