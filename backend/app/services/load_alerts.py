"""관절 과부하 경보를 **화면까지** 가져온다.

## 왜 이게 따로 필요했나

robotd 는 과부하를 정확히 잡아내고 있었는데 (2026-09-09 10:22:39, can0 joint2
11.4N·m/9.7A) 사람은 아무것도 못 봤다 — `logger.warning` 이 journald 로만
나갔기 때문이다. **감지와 통보는 다른 일이고, 감지만 만들면 아무도 모른다.**

## ⚠ `device_watch` 의 경보 목록에 합류시키지 않는다

거기 경보는 **상태**다: 장치가 없어졌다 → 돌아왔다. 그래서 `DeviceAlerts` 가
매번 `clear(PREFIX)` 로 목록을 갈아끼우고, 조건이 풀리면 메시지도 사라진다.

과부하는 **사건**이다. 실기에서 J2 는 19초 동안 임계 위에 있다가 내려왔고,
2분 뒤 상태를 봤을 때는 이미 `raised=false` 였다. 상태로 다루면 그 배너는
잠깐 떴다 스스로 사라진다 — 자리를 비웠던 사람에게는 아무 일도 없던 것과 같다.
알아야 할 것은 "지금 눌리고 있다" 가 아니라 **"눌렸었다"** 다.

그래서 사건마다 고유 id 를 주고, 지울지는 사람이 정한다.

## 왜 폴링인가

경보는 `events` 카운터의 증가로 판정한다. robotd 가 채널로 밀어주면 더 즉각적
이겠지만 게이트웨이에 첫 구독자를 들이게 되고, 이 알림은 안전 경로가 아니다
(안전 정지는 estopd 가 따로 한다). 2초 폴링이면 충분하고, 카운터를 보므로
**두 폴링 사이에 뜨고 진 경보도 안 놓친다** — 상태를 보면 놓쳤을 것이다.
"""

import logging

logger = logging.getLogger(__name__)


class LoadAlertWatch:
    """팔·관절별 경보 카운터를 좇는다. 증가분만 새 사건이다."""

    def __init__(self) -> None:
        #: (iface, joint) → 마지막으로 본 경보 횟수
        self._seen: dict[tuple[str, str], int] = {}

    def check(self) -> list[dict]:
        """연결된 팔을 훑어 **새로 생긴** 과부하 사건을 돌려준다."""
        from app.services import robot_manager as rm

        try:
            # ⚠ **팔마다 부르지 않는다.** 이 루프는 2초마다 도는데 RPC 하나가
            #   최대 20초까지 기다린다 — 팔 4대면 장치 감시가 통째로 멎는다.
            snaps = rm.load_status_all()
        except Exception as exc:                            # noqa: BLE001
            logger.debug("부하 상태를 못 읽었습니다: %s", exc)
            return []
        out: list[dict] = []
        for iface, snap in (snaps or {}).items():
            out += self._diff(iface, snap)
        return out

    def _diff(self, iface: str, snap: dict | None) -> list[dict]:
        if not snap:
            return []
        out: list[dict] = []
        for joint, st in (snap.get("joints") or {}).items():
            key = (iface, joint)
            count = int(st.get("events") or 0)
            before = self._seen.get(key)
            self._seen[key] = count
            # ⚠ **처음 본 팔은 알리지 않는다.** 게이트웨이가 재시작되면 데몬의
            #   카운터는 그대로라, 기준을 안 잡으면 부팅하자마자 지난 사건들이
            #   방금 난 것처럼 쏟아진다.
            if before is None:
                continue
            # 리셋(0x150)이 창을 닫으면 카운터가 0 으로 돌아간다 — 줄어든 것은
            # 사건이 아니라 새 창이다. 조용히 기준만 다시 잡는다.
            if count <= before:
                continue
            out.append({
                "id": f"{iface}:{joint}:{st.get('last_at') or count}",
                "iface": iface,
                "joint": joint,
                # ⚠ **문구는 잰 쪽이 만든다.** 경보가 선 순간의 토크·지속시간은
                #   그 뒤 표본에 덮여 사라지므로, 여기서 조립하면 경보 때와 다른
                #   숫자를 말하게 된다 (`JointLoad.last_text`).
                "text": st.get("last_text") or (
                    f"{iface} {joint}: 과부하 경보 (상세는 로봇 › 상세 › 부하)"),
                "peak_nm": st.get("peak_nm"),
                "over_s": st.get("over_s"),
                "at": st.get("last_at"),
            })
        return out

    def forget(self, iface: str) -> None:
        """이 팔의 기준을 버린다 — 연결이 끊겼을 때. 남겨두면 다시 붙었을 때
        데몬의 새 카운터(0)와 옛 기준을 비교하게 된다."""
        for key in [k for k in self._seen if k[0] == iface]:
            self._seen.pop(key, None)


load_alert_watch = LoadAlertWatch()
