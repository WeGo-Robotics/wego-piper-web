"""장치가 사라진 것을 알아채고 **한 번** 알린다 — CAN 이든 카메라든.

## 왜 필요한가

USB 가 빠지거나 xHCI 컨트롤러가 죽으면(이 저장소에서 실제로 겪었다) 장치가 통째로
사라지는데, 화면은 그대로였다. 목록은 마지막에 본 상태에 머물고, 추론만 뒤늦게
"세그먼트가 없습니다"로 죽었다. **화면과 에러가 서로 다른 말을 하면 원인을 못 찾는다.**

## 누가 판정하는가 — **장치를 쥔 데몬이** 한다

가장 좋은 신호는 소유자에게서 온다. camerad 는 `/dev/videoN` 노드가 사라진 것을
1초마다 보고, rsd 는 파이프라인이 5초간 프레임을 못 내면 결론을 낸다. 그러면
데몬이 발행을 끊고 `lost()` 로 알려준다 — **추론이 아니라 사실**이다.

게이트웨이의 세그먼트 신선도 판정은 **보조**로 남는다: 데몬이 통째로 죽거나
얼어붙어(SIGSTOP·D-state) 스스로 결론을 못 내는 경우가 그것으로 잡힌다.

## 보조 판정 — 발행이 **멈췄는가** (존재가 아니라)

처음에는 "세그먼트가 없으면 사라진 것"으로 봤는데, **실기에서 아무 반응이 없었다.**
USB 를 뽑아도 세그먼트는 남아 있기 때문이다:

    # cam/piper_cam/hub.py — 읽기 루프
    ok, frame = self._cap.read()
    if ok and frame is not None:
        self._publish(frame)          # ← 실패하면 발행만 안 할 뿐

장치가 빠지면 `cap.read()` 가 계속 실패하고, 루프는 돌지만 아무것도 발행하지 않는다.
`stop_publish()` 는 **명시적인 `disconnect()` 에서만** 불리므로 파일은 그대로 남는다.
세그먼트가 사라지는 것은 데몬을 정상 종료했을 때뿐이고, 그래서 `systemctl stop` 으로 한
검증은 통과하는데 진짜 USB 뽑기는 못 잡았다.

그래서 헤더의 `wall_ns`(마지막 발행 시각)를 본다 — **얼마나 오래됐는가.**
세그먼트가 없어진 경우도 자동으로 포함된다(열 수 없으면 무한대로 친다).
발행자와 게이트웨이는 같은 호스트라(컨테이너도 호스트 시계를 쓴다) 벽시계 비교가 성립한다.

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

## 없어진 세그먼트는 안 본다 — **남아 있는데 멈춘 것**만 본다

한동안 "전에 봤는데 지금 없다"도 경보로 쳤다. 그러자 rsd 를 재시작하고 카메라를
아직 안 열었을 뿐인 **정상 유휴 상태**가 "사라졌습니다"로 떴다. 세그먼트가 없는 데는
이유가 둘이고(닫혀 있다 / 없어졌다) 세그먼트만으로는 못 가른다.

그래서 신호를 하나로 좁혔다: **세그먼트가 있는데 멈춰 있다.** 이건 이유가 하나뿐이다 —
발행자가 비정상으로 멈췄다. 닫으면 파일이 지워지고(`stop_publish`), 데몬은 기동할 때
남은 것을 치우므로, 남아 있으면서 멈춘 것은 언제나 사고다.

돌고 있던 카메라를 뽑는 것이 정확히 이 모양이다 — `cap.read()` 만 실패하고 파일은
남는다. 반대로 **안 열어둔 카메라를 뽑는 것은 못 잡는다.** 그건 스캔이 잡고
(`present: false`), 녹화 시작이 이름을 대며 거부한다. 즉시성이 필요한 쪽은
"쓰던 중에 빠졌다" 이고, 그건 여기서 잡힌다.

## 전이에서만 알린다

같은 사실을 2초마다 반복해서 띄우면 아무도 안 읽는다. 나타남↔사라짐이 바뀔 때만
방송하고, 현재 목록은 API 로 따로 준다 (나중에 페이지를 연 사람도 봐야 한다).
"""

import logging
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

# 이만큼 새 프레임이 없으면 발행이 멈춘 것으로 본다.
# 가장 느린 정상 스트림(깊이 15fps)의 여러 배 — 한 프레임 늦었다고 경보를 내면
# 부하가 튈 때마다 거짓 경보가 뜬다. 감시 주기(2초)보다도 커야 한다.
STALE_S = 5.0

# ── 문구는 여기서만 만든다 ──
# 화면이 문장을 따로 조립하면 한쪽만 고쳐져 어긋난다 (`_usb_warning` 과 같은 규칙).


def _survey_arms() -> tuple[set[str], set[str]]:
    """`(발행 중, 멈춘 채 남아 있는)` 팔."""
    from piper_shm.arm import StateReader, list_segments

    fresh, stale = set(), set()
    for name in list_segments():
        if not name.endswith(".state"):
            # `.action` 은 소비자가 쓰는 것 — 팔이 살아 있다는 증거가 아니다
            continue
        iface = name.removesuffix(".state")
        try:
            reader = StateReader(iface)
        except Exception:
            continue
        try:
            (fresh if reader.age_s() <= STALE_S else stale).add(iface)
        finally:
            close = getattr(reader, "close", None)
            if close:
                close()
    return fresh, stale


def _survey_cameras() -> tuple[set[str], set[str]]:
    """`(발행 중, 멈춘 채 남아 있는)` 카메라 세그먼트."""
    import time

    from piper_shm import Subscriber, list_segments

    now = time.time_ns()
    fresh, stale = set(), set()
    for name in list_segments():
        try:
            sub = Subscriber(name)
        except Exception:
            continue
        try:
            wall = sub.wall_ns()
            age = (now - wall) / 1e9 if wall else float("inf")
            (fresh if age <= STALE_S else stale).add(name)
        except Exception:
            pass
        finally:
            sub.close()
    return fresh, stale


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
                 f"{what} {name} 의 영상이 끊겼습니다 ({ident}). "
                 "USB 연결을 확인하세요 — 뽑혔거나 컨트롤러가 내려갔을 수 있습니다. "
                 "이 상태로 녹화·추론을 시작하면 시작하자마자 실패합니다.")


def _all_gone(kind: str, daemon: str, count: int) -> Alert:
    """전부 한꺼번에 사라졌다 — 개별 USB 문제로 보기 어렵다."""
    what = "로봇팔" if kind == "robot" else "카메라"
    return Alert(kind, f"all:{daemon}", daemon, "all_gone",
                 f"{what} {count}개가 **한꺼번에** 사라졌습니다. 개별 USB 문제가 아닙니다 — "
                 f"`systemctl --user status piper-{daemon}` 로 데몬을 먼저 보고, "
                 "데몬이 멀쩡하면 USB 컨트롤러가 내려간 것입니다(`dmesg | tail`).")


def _stalled(kind: str, ident: str, name: str) -> Alert:
    """장치는 **있는데** 발행이 멈췄다 — 케이블 문제가 아니다.

    USB 를 확인하라고 하면 있는 장치를 뽑으러 가게 만든다. 실제로 그 오보를 냈다:
    노트북 내장 웹캠(뽑을 수도 없는 것)에 "USB 연결을 확인하세요" 가 떴다.
    """
    what = "로봇팔" if kind == "robot" else "카메라"
    return Alert(kind, ident, name, "stalled",
                 f"{what} {name} 의 발행이 멈췄습니다 ({ident}). **장치는 꽂혀 있습니다** — "
                 "데몬이 스트림을 놓친 것이라 다시 연결하면 됩니다. "
                 "이 상태로 녹화·추론을 시작하면 시작하자마자 실패합니다.")


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

    def alerts(self) -> list[dict]:
        return [a.to_dict() for a in self._alerts]

    def summary(self) -> dict:
        """상태바용 한 줄 요약 — **세는 것이 아니라 이미 아는 것을 내놓는다.**

        `check()` 가 2초마다 조사하고 그 결과를 `_apply_to_managers()` 로 관리자에
        내려놓는다. 그러니 관리자의 `connected` 는 이미 최신이다. 여기서 장치를
        다시 열거하면 같은 일을 두 번 하는 것이고, 무엇보다 **판정이 두 벌이 된다** —
        경보는 "없어졌다"인데 개수는 "정상"인 상태가 만들어진다.

        - `ok`   등록해뒀고 **거기 있는** 것
        - `warn` 등록해뒀는데 없어진 것

        등록 안 한 장치는 안 센다. 스캔에 뜨는 것과 **이 시스템이 쓰기로 한 것**은
        다르고, 상태바가 답해야 하는 것은 후자다.

        ⚠ **"안 열려 있다"는 경고가 아니다.** 카메라는
        [`present && !connected` 를 정상으로 정의](camera_manager.py)해 뒀다 —
        꽂혀 있는데 아직 안 연 상태이고, 녹화·추론이 시작할 때 `prepare_cameras`
        가 그때 연다. 이걸 경고로 세면 **게이트웨이를 재시작할 때마다 등록된
        카메라가 전부 노랗게 뜬다.** 같은 실수를 경보 쪽에서 이미 한 번 했다
        (`test_idle_is_not_a_fault` — rsd 를 되살렸는데 아직 안 연 것이
        "사라졌습니다"로 떴다).

        팔에는 그 구분이 없다. `ArmInfo` 는 `present` 를 안 갖고, `connected` 가
        곧 "robotd 가 이 팔을 안다"이며 `mark_absent()` 가 지우는 것도 그것이다.
        """
        from app.services.camera_manager import camera_manager
        from app.services.robot_manager import robot_manager

        def _here(device) -> bool:
            """이 장치가 거기 있는가."""
            if hasattr(device, "present"):
                return bool(device.present)   # 카메라 — 안 연 것과 없는 것을 가른다
            return bool(getattr(device, "connected", False))   # 팔 — robotd 가 아는가

        def _count(items) -> dict:
            registered = [d for d in items if getattr(d, "ready", False)]
            ok = sum(1 for d in registered if _here(d))
            return {"ok": ok, "warn": len(registered) - ok}

        return {
            "robots": _count(robot_manager.arms.values()),
            "cameras": _count(camera_manager.cameras.values()),
            "alerts": len(self._alerts),
        }

    def check(self) -> tuple[list[Alert], list[Alert]]:
        """지금 상태를 보고 `(새로 생긴 것, 해소된 것)` 을 돌려준다."""
        found = self._collect()
        # ⚠ **판정을 목록에도 반영한다.** 경보는 떴는데 카메라 페이지는 그 장치를
        # 멀쩡한 것처럼 보여주던 문제가 있었다 — `present` 가 사용자가 스캔을
        # 누를 때만 갱신됐기 때문이다. 여기가 이미 알고 있으니 여기서 내린다.
        self._apply_to_managers(found)
        keys = {(a.kind, a.ident) for a in found}
        new = [a for a in found if (a.kind, a.ident) not in self._seen]
        gone = [a for a in self._alerts if (a.kind, a.ident) not in keys]
        self._seen, self._alerts = keys, found
        return new, gone

    def _apply_to_managers(self, alerts: list[Alert]) -> None:
        """사라진 장치를 관리자 목록에서도 "없음"으로 내린다.

        등록·별칭은 사람이 정한 것이라 남긴다 (`mark_absent` 가 장치 사실만 지운다).
        실패해도 경보를 막지 않는다 — 알리는 것이 본업이다.
        """
        gone = {(a.kind, a.ident) for a in alerts if a.reason in ("device_gone", "all_gone")}
        if not gone:
            return
        try:
            from app.services.camera_manager import camera_manager
            from app.services.robot_manager import robot_manager

            for kind, ident in gone:
                target = (camera_manager.cameras if kind == "camera"
                          else robot_manager.arms).get(ident)
                if target is not None:
                    target.mark_absent()
        except Exception as exc:
            logger.debug("판정 반영 실패: %s", exc)

    # ── 무엇이 사라졌나 ──

    def _collect(self) -> list[Alert]:
        out: list[Alert] = []
        out += self._robots()
        out += self._cameras()
        return out

    def _robots(self) -> list[Alert]:
        from app.services.robot_manager import (
            lost_arms, robot_manager, robotd_available)

        arms = robot_manager.arms

        def _name(iface: str) -> str:
            arm = arms.get(iface)
            return arm.role if arm and arm.role != "unknown" else iface

        # ⚠ **데몬이 판정한 것을 먼저 쓴다.** robotd 는 `can0` 인터페이스가 사라진 것을
        # 1초 안에 보지만, 게이트웨이는 컨테이너라 `/sys/class/net` 자체가 안 보인다.
        try:
            declared = [_device_gone("robot", i["id"], _name(i["id"]))
                        for i in lost_arms() if i.get("id")]
        except Exception as exc:
            logger.debug("robotd lost() 조회 실패: %s", exc)
            declared = []
        if declared:
            return declared

        try:
            alive, stopped = _survey_arms()
        except Exception as exc:
            logger.debug("팔 신선도 조회 실패: %s", exc)
            return []

        missing = sorted(stopped)
        if not missing:
            # 발행이 아무것도 없다 — 쥐고 있다고 **기록된** 팔이 있는데 데몬이 죽었으면
            # 그건 알린다. 팔을 안 연 유휴 상태와는 다르다.
            held = [a for a in robot_manager.arms.values() if a.ready]
            if held and not robotd_available():
                return [_daemon_down("robot", "robotd")]
            return []
        if not robotd_available():
            return [_daemon_down("robot", "robotd")]
        # ⚠ **둘 이상일 때만** "한꺼번에" 다. 하나뿐이면 "전부"와 "그 하나"를
        # 구분할 수 없고, 그때는 그 장치의 USB 를 보라는 쪽이 쓸모 있다.
        if len(missing) >= 2:
            return [_all_gone("robot", "robotd", len(missing))]

        return [_device_gone("robot", i, _name(i)) for i in missing]

    def _cameras(self) -> list[Alert]:
        from app.services.camera_manager import camera_manager
        from app.services.realsense_manager import realsense_hub, rs_available
        from app.services.v4l2_client import v4l2_hub

        all_cams = camera_manager.cameras
        # ⚠ **쓰고 있는 것만 본다.** 스캔 probe 가 남긴 썸네일 세그먼트는 태어날 때부터
        # 멈춰 있어서, 안 거르면 스캔만 눌러도 "발행이 멈췄다"가 뜬다.
        # (데몬 생존 판정은 아래에서 `all_cams` 를 쓴다 — 등록만 해둔 것도 세야 한다.)
        cams = {cid: c for cid, c in all_cams.items() if c.connected}

        def _name(cam_id: str) -> str:
            cam = cams.get(cam_id)
            return (cam.label or cam.name) if cam else cam_id

        # ⚠ **데몬이 판정한 것을 먼저 쓴다.** 장치를 쥔 쪽이 노드 사라짐을 1초 안에
        # 보므로 여기서 세그먼트로 추론하는 것보다 빠르고 확실하다.
        # 아래 신선도 판정은 데몬이 스스로 결론을 못 내는 경우(얼어붙음)용 보조다.
        declared: list[Alert] = []
        for hub in (realsense_hub, v4l2_hub):
            try:
                for item in hub.lost():
                    cid = item.get("id", "")
                    if cid:
                        declared.append(_device_gone("camera", cid, _name(cid)))
            except Exception as exc:
                logger.debug("lost() 조회 실패: %s", exc)
        if declared:
            return declared

        try:
            from piper_shm import segment_for_camera
            alive, stopped = _survey_cameras()
        except Exception as exc:
            logger.debug("카메라 신선도 조회 실패: %s", exc)
            return []

        # 세그먼트 이름 ↔ 카메라 id. 사라진 뒤에도 이름을 말하려면 매핑이 필요한데,
        # 관리자에서 사라진 카메라는 세그먼트 이름밖에 안 남는다.
        seg_of = {c.id: segment_for_camera(c.id) for c in cams.values()}
        id_of = {seg: cid for cid, seg in seg_of.items()}
        # 관리자가 모르는 세그먼트는 우리 것이 아니다 — 남의 잔재나 테스트 산물이다
        stopped = {s for s in stopped if s in id_of}
        alive = {s for s in alive if s in id_of}

        missing = sorted(stopped)
        if not missing:
            out: list[Alert] = []
            for kind, daemon in (("realsense", "rsd"), ("opencv", "camerad")):
                held = [c for c in all_cams.values() if c.ready and c.cam_type == kind]
                alive_daemon = (rs_available() if kind == "realsense"
                                else v4l2_hub.available())
                if held and not alive_daemon:
                    out.append(_daemon_down("camera", daemon))
            return out

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
            if len(mine_missing) >= 2:
                out.append(_all_gone("camera", daemon, len(mine_missing)))
                continue
            for seg in mine_missing:
                cam = cams.get(id_of.get(seg, ""))
                cid = cam.id if cam else seg
                label = (cam.label or cam.name) if cam else seg
                # ⚠ **스캔이 장치를 봤으면 케이블 탓이 아니다.** 이 판정은 세그먼트
                # 신선도만 보는 보조라 "왜 멈췄나"를 모른다 — 장치 존재 여부가
                # 그걸 가른다. 안 가르면 내장 웹캠에 "USB 를 확인하세요" 가 뜬다.
                if cam is not None and getattr(cam, "present", True):
                    out.append(_stalled("camera", cid, label))
                else:
                    out.append(_device_gone("camera", cid, label))
        return out


device_watch = DeviceWatch()
