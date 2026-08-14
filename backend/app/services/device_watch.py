"""장치가 사라진 것을 알아채고 **한 번** 알린다 — CAN 이든 카메라든.

## 왜 필요한가

USB 가 빠지거나 xHCI 컨트롤러가 죽으면(이 저장소에서 실제로 겪었다) 장치가 통째로
사라지는데, 화면은 그대로였다. 목록은 마지막에 본 상태에 머물고, 추론만 뒤늦게
"세그먼트가 없습니다"로 죽었다. **화면과 에러가 서로 다른 말을 하면 원인을 못 찾는다.**

## 판정 근거 — 세그먼트가 곧 임대권

이 저장소는 이미 "세그먼트 존재 = 누군가 그 장치를 쥐고 있다"로 돌아간다.
그래서 사라짐도 같은 자리에서 본다: **쥐고 있던 장치의 발행이 끊겼다.**
장치 열거(`scan`)를 주기적으로 돌릴 필요가 없다 — `/dev/shm` 을 훑는 것은
사실상 공짜고, RPC 도 안 탄다.

## 데몬이 죽은 것과 장치가 빠진 것은 다르다

robotd 가 죽어도 세그먼트는 사라진다. 그때 "팔이 빠졌다"고 하면 거짓말이다 —
USB 를 확인하러 가게 만든다. 그래서 데몬 생존을 **먼저** 보고 문구를 가른다.

⚠ 그런데 **생존 표시는 3초 늦게 만료된다**(`DAEMON_ALIVE_TTL_MS`). 세그먼트는 즉시
사라지므로 그 사이엔 "데몬은 살아 있는데 장치만 없다"로 보인다 — robotd 를 멈춰
실험했더니 정확히 그랬다: 먼저 "USB 를 확인하세요"라 하고 3초 뒤에 "데몬이 내려갔다"로
바뀌었다.

그래서 **한꺼번에 전부 사라진 경우를 따로 다룬다.** 쥐고 있던 장치가 남김없이 같은
순간에 없어지는 것은 개별 USB 문제가 아니다 — 발행자가 멈췄거나(데몬) USB 컨트롤러가
통째로 내려간 것이다(xHCI HC died — 이 저장소에서 겪었다). 둘 중 어느 쪽인지는
확인 방법이 다르므로 **둘 다 적어준다.** 하나를 골라 틀리게 말하는 것보다 낫다.

## 무엇을 "쥐고 있었다"로 볼 것인가 — 관리자 플래그가 아니다

처음에는 `connected` 플래그를 봤는데, **스캔이 그 플래그를 내린다.** 그래서 장치가
빠진 뒤 스캔을 누르면 경보가 사라졌다 — 정작 장치는 여전히 없는데. 실기에서 바로 걸렸다.

그래서 **발행을 본 적이 있는지**를 여기서 직접 기억한다. 한 번이라도 세그먼트를
봤던 장치가 안 보이면 사라진 것이다. 관리자 상태와 무관하므로 스캔·재연결이
판정을 흔들지 못한다. 게이트웨이를 새로 띄우면 기억이 비어 있는데, 그것도 맞다 —
**본 적 없는 장치를 잃었다고 하지 않는다.**

## 전이에서만 알린다

같은 사실을 2초마다 반복해서 띄우면 아무도 안 읽는다. 나타남↔사라짐이 바뀔 때만
방송하고, 현재 목록은 API 로 따로 준다 (나중에 페이지를 연 사람도 봐야 한다).
"""

import logging
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

# ── 문구는 여기서만 만든다 ──
# 화면이 문장을 따로 조립하면 한쪽만 고쳐져 어긋난다 (`_usb_warning` 과 같은 규칙).


@dataclass(frozen=True)
class Alert:
    kind: str        # "robot" | "camera"
    ident: str       # iface 또는 cam_id
    name: str        # 사람이 알아볼 이름 (별칭·역할)
    reason: str      # "device_gone" | "daemon_down"
    text: str

    def to_dict(self) -> dict:
        return {"kind": self.kind, "id": self.ident, "name": self.name,
                "reason": self.reason, "text": self.text}


def _device_gone(kind: str, ident: str, name: str) -> Alert:
    what = "로봇팔" if kind == "robot" else "카메라"
    return Alert(kind, ident, name, "device_gone",
                 f"{what} {name} 이(가) 사라졌습니다 ({ident}). "
                 "USB 연결을 확인하세요 — 뽑혔거나 컨트롤러가 내려갔을 수 있습니다.")


def _all_gone(kind: str, daemon: str, count: int) -> Alert:
    """전부 한꺼번에 사라졌다 — 개별 USB 문제로 보기 어렵다."""
    what = "로봇팔" if kind == "robot" else "카메라"
    return Alert(kind, f"all:{daemon}", daemon, "all_gone",
                 f"{what} {count}개가 **한꺼번에** 사라졌습니다. 개별 USB 문제가 아닙니다 — "
                 f"`systemctl --user status piper-{daemon}` 로 데몬을 먼저 보고, "
                 "데몬이 멀쩡하면 USB 컨트롤러가 내려간 것입니다(`dmesg | tail`).")


def _daemon_down(kind: str, daemon: str) -> Alert:
    what = "로봇팔" if kind == "robot" else "카메라"
    return Alert(kind, f"daemon:{daemon}", daemon, "daemon_down",
                 f"{daemon} 데몬이 응답하지 않습니다 — {what}을(를) 쓸 수 없습니다. "
                 f"장치가 빠진 것이 아니라 데몬이 내려간 것입니다 "
                 f"(`systemctl --user status piper-{daemon}`).")


@dataclass
class DeviceWatch:
    """마지막으로 본 경보 집합. 전이에서만 방송한다."""

    _seen: set[tuple[str, str]] = field(default_factory=set)
    _alerts: list[Alert] = field(default_factory=list)
    # 한 번이라도 발행을 본 장치. **관리자 플래그가 아니라 여기가 근거다** — 위 참고.
    _published: dict[str, set[str]] = field(
        default_factory=lambda: {"robot": set(), "camera": set()})

    def forget(self, kind: str, ident: str) -> None:
        """사용자가 일부러 끊었다 — 사라진 것이 아니므로 기억에서 지운다.

        ⚠ 기억은 **세그먼트 이름**으로 들고 있는데 호출부는 카메라 id 를 준다
        (`rs:1:color` vs `rs_1_color`). 여기서 맞춰주지 않으면 지워지지 않고,
        프리뷰를 끌 때마다 "사라졌습니다" 가 뜬다 — 실기에서 걸렸다.
        """
        key = ident
        if kind == "camera":
            try:
                from piper_shm import segment_for_camera

                key = segment_for_camera(ident)
            except Exception:
                pass
        self._published.get(kind, set()).discard(key)

    def alerts(self) -> list[dict]:
        return [a.to_dict() for a in self._alerts]

    def check(self) -> tuple[list[Alert], list[Alert]]:
        """지금 상태를 보고 `(새로 생긴 것, 해소된 것)` 을 돌려준다."""
        found = self._collect()
        keys = {(a.kind, a.ident) for a in found}
        new = [a for a in found if (a.kind, a.ident) not in self._seen]
        gone = [a for a in self._alerts if (a.kind, a.ident) not in keys]
        self._seen, self._alerts = keys, found
        return new, gone

    # ── 무엇이 사라졌나 ──

    def _collect(self) -> list[Alert]:
        out: list[Alert] = []
        out += self._robots()
        out += self._cameras()
        return out

    def _robots(self) -> list[Alert]:
        from app.services.robot_manager import robot_manager, robotd_available

        try:
            from piper_shm.arm import list_segments
            # ⚠ 팔 세그먼트는 `can0.state` 처럼 **접미사가 붙어** 온다
            # (`.state` = robotd 발행, `.action` = 소비자 발행). 상태를 안 흘리면
            # 그 팔은 죽은 것이므로 `.state` 만 본다 — 접미사를 안 맞추면
            # 모든 팔이 항상 "사라졌다"가 된다.
            alive = {n.removesuffix(".state") for n in list_segments()
                     if n.endswith(".state")}
        except Exception as exc:
            logger.debug("팔 세그먼트 조회 실패: %s", exc)
            return []

        known = self._published["robot"]
        known |= alive
        missing = sorted(known - alive)
        if not missing:
            return []
        if not robotd_available():
            return [_daemon_down("robot", "robotd")]
        # ⚠ **둘 이상일 때만** "한꺼번에" 다. 하나뿐이면 "전부"와 "그 하나"를
        # 구분할 수 없고, 그때는 그 장치의 USB 를 보라는 쪽이 쓸모 있다.
        if len(missing) == len(known) and len(missing) >= 2:
            return [_all_gone("robot", "robotd", len(missing))]

        arms = robot_manager.arms
        def _name(iface: str) -> str:
            arm = arms.get(iface)
            return arm.role if arm and arm.role != "unknown" else iface

        return [_device_gone("robot", i, _name(i)) for i in missing]

    def _cameras(self) -> list[Alert]:
        from app.services.camera_manager import camera_manager
        from app.services.realsense_manager import rs_available
        from app.services.v4l2_client import v4l2_hub

        try:
            from piper_shm import list_segments, segment_for_camera
            alive = set(list_segments())
        except Exception as exc:
            logger.debug("카메라 세그먼트 조회 실패: %s", exc)
            return []

        cams = camera_manager.cameras
        # 세그먼트 이름 ↔ 카메라 id. 사라진 뒤에도 이름을 말하려면 매핑이 필요한데,
        # 관리자에서 사라진 카메라는 세그먼트 이름밖에 안 남는다.
        seg_of = {c.id: segment_for_camera(c.id) for c in cams.values()}
        id_of = {seg: cid for cid, seg in seg_of.items()}

        known = self._published["camera"]
        known |= alive
        missing = sorted(known - alive)
        if not missing:
            return []

        def _kind(seg: str) -> str:
            cam = cams.get(id_of.get(seg, ""))
            return cam.cam_type if cam else ("realsense" if seg.startswith("rs_") else "opencv")

        out: list[Alert] = []
        by_type = {"realsense": rs_available(), "opencv": v4l2_hub.available()}
        # 데몬별로 따로 센다 — rsd 가 죽어도 웹캠은 멀쩡할 수 있고, 그때
        # "카메라 전부 사라짐"이라고 하면 거짓말이다.
        for kind, daemon in (("realsense", "rsd"), ("opencv", "camerad")):
            mine_missing = [s for s in missing if _kind(s) == kind]
            if not mine_missing:
                continue
            if not by_type[kind]:
                out.append(_daemon_down("camera", daemon))
                continue
            mine_known = [s for s in known if _kind(s) == kind]
            if len(mine_missing) == len(mine_known) and len(mine_missing) >= 2:
                out.append(_all_gone("camera", daemon, len(mine_missing)))
                continue
            for seg in mine_missing:
                cam = cams.get(id_of.get(seg, ""))
                cid = cam.id if cam else seg
                out.append(_device_gone("camera", cid,
                                        (cam.label or cam.name) if cam else seg))
        return out


device_watch = DeviceWatch()
