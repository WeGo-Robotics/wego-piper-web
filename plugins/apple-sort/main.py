#!/usr/bin/env python3
"""apple-sort — 작업판 위 사과를 검출해, 남은 종류에 맞는 지시문(task)을 언어 정책에 넣는다.

`dualarm_sort_apple_stage` 로 학습한 SmolVLA-S 는 에피소드 하나에 사과 하나를 옮겼고, 어느
종류인지는 지시문이 정했다("put the red apple in the left box"). 추론 중에 사람이 화면에서
지시문을 바꿔 주던 일을 `MissionPlugin` 훅으로 대신한다:

    observe  팔이 쉬고(관절 정지 + 그리퍼 닫힘) 판 위 개수가 `still_s` 동안 그대로일 때 센다
    decide   규칙 순서대로, 판에 남은 첫 종류의 지시문 — `{arm}` 은 고른 사과 쪽 팔로 채운다
    execute  set_task — 같은 종류·같은 쪽이 남았으면 같은 문장이다 (정책이 다음 것을 집는다)
    done     판 위 개수가 바뀌고 팔이 다시 쉬면 — 옮기는 도중에는 끝나지 않는다
    record   전후 개수·목표 종류를 저널에 (`.runtime/runs/*.jsonl`)

**지시문은 팔이 쉬는 동안에만 바뀐다.** 잡는 도중에 문장이 바뀌는 건 학습에 없던 관측이다.
종류가 바닥나도 정책은 가만히 있는다 — 데이터셋의 `empty_target` 변형(15개)이 그렇게 가르쳤다
— 그 쉼을 보고 다음 종류로 넘어간다. 같은 종류·팔이 `max_fail` 번 연속 타임아웃이면 그 판에서는
반대쪽 팔·다음 종류를 먼저 한다 (판 위 개수가 바뀌면 다시 센다).

**예외 — 지시문이 판과 어긋나면 쉼을 기다리지 않는다** (`stale_s`, `_watch_task`). 대상 사과가 사라지거나
(사람이 치움·떨어짐) 반대쪽으로 옮겨지면 정책은 옛 문장대로 계속 움직여 쉼이 안 잡히고, 쉼이 안 잡히니
문장도 안 바뀐다(2026-11-01 117). 그래서 매 프레임 본다 — 대상(그 팔 쪽의 그 종류)이 판에 없고 그 팔이
쥐고 있지 않은 채로 `stale_s` 가 지나면 추론을 일시정지하고 시작 직후처럼(`_start_up`) 판을 다시 세어 맞는
지시문 → 원위치 리셋 → 재개. 판이 비어 보이면 멈춘 팔이 사과를 가렸을 수 있으니 원위치로 비켜서 한 번 더
세고(`_home_and_hold`), 그래도 비었으면 멈춘 채 사과를 기다린다. 쥐었다 놓은 뒤에는 원위치로 돌아와 쉴
시간을 더 준다(`after_release_s`) — 놓은 것으로 끝이 아니다: 117 에서 왼팔이 사과를 옮기고 원위치에 1.4초
머문 뒤(쉼 1.5초를 못 채워 완료가 안 났다) 빈 자리를 50초 넘게 다시 집으러 갔다. "쥐었다" 는 그리퍼가
`grip_closed` 와 `grip_open` 사이에서 멈춘 것이다(`_holding`) — 천천히 열며 그 사이를 지나가는 값은 아니다.
⚠ `grip_open` 은 그리퍼 행정이 데이터셋과 같다는 전제다(117 팔로워 100mm — 70mm 면 쥔 사과가 86 으로 읽혀
"열림" 이 된다).

**팔은 지시문이 정한다** (`{arm}` 규칙). 같은 종류가 판 양쪽에 있으면 종류만 말해서는 어느 팔이 갈지
안 정해진다 — 데이터셋에 그런 에피소드가 36개, 어느 쪽을 집을지는 시나리오 계획이 정해서 화면만 보는
정책에게는 반반이었다(2026-10-31). 그래서 task 를 "with the left arm, put …" 로 다시 매겼고, 여기서는 사과
중심이 `arm_split` 보다 왼쪽이면 left 다 — 쉼 창(`still_s`) 프레임의 과반에서 그쪽에 보여야 한다. 0.52 는 판 E|F 경계의 화면 x(위 카메라 프레임의 열 간격)이고,
대상 종류가 하나뿐인 에피소드 220개에서 사람이 고른 팔과 214개가 맞았다 — 나머지 6개는 경계(x 0.48~0.62)에서
반대 팔을 쓴 것. 양쪽에 있으면 상자 쪽 팔을 먼저 시킨다(덜 뻗는다). `{arm}` 이 없는 규칙은 예전 그대로다 —
팔을 안 밝히고 학습한 체크포인트용.

**판이 두 번 연속 비면** 설정 두 개로 갈린다 — `on_empty_inference`: 추론을 일시정지(pause, 사과가 다시
올라오면 지시문을 넣고 재개) · 그대로 둠(wait) · 정지(stop_inference, 플러그인도 끝). `on_empty_plugin`:
끝냄(stop, 우리가 추론을 쉬게 했으면 task 를 공백으로) · 계속 대기(wait, 사과가 다시 올라오면 이어서).

쉼 판정 숫자는 데이터셋 실측이다 (2026-10-31, phase 라벨 IDLE·DONE vs 나머지, 1초 창):
관절 변동 중앙값 0 / 움직임 하위 5% 6.6 → `still_tol` 2, 그리퍼 쉴 때 <1 · 잡고 있을 때 ≥28
→ `grip_closed` 10.

검출 모델은 검출 데이터셋 `dualarm_sort_apple`(에피소드 위 카메라 프레임을 자동 라벨하고
시나리오 계획의 판 위 개수로 검증 — `tools/autolabel.py`)로 학습한 RT-DETR 이다. 라벨 규칙이
"작업판 위 사과만"이라 상자 안 사과는 원래 안 잡힌다. ROI 는 이중 안전선이다.
`dualarm_sort_apple-1031-0501` 은 상자·순위는 맞는데 **점수가 낮다** — 참 검출 0.09~0.2,
잡음 ≤0.07 (학습 루프가 mAP50 포화 뒤 19에폭을 남겼고 새 헤드가 덜 자랐다). 그래서 conf 0.08 +
클래스 무관 NMS 다. 그 설정으로 라벨된 587 프레임의 판 위 개수 586 이 맞았고(학습에 쓴 프레임이라
낙관치), 라벨 못 한 13 장 중 틀린 7 장은 전부 팔이 사과를 가리거나 든 장면이다 — 쉴 때만 세므로
판단에는 안 걸린다. 모델을 바꾸면 conf 부터 다시 잰다 (`tools/replay.py`·`tools/autolabel.py`).

추론은 먼저 켜 둔다 — 가능하면 **학습한 문장 중 하나로** (아무 문장이나 넣으면 첫 쉼까지 정책이
무엇을 할지 모른다). 이전 실행이 일시정지로 끝났어도, 지시문 없이 시작했어도 된다: 시작하면
**팔 쉼을 기다리지 않고** 판 위 개수가 안정되는 대로(`still_s`) 첫 판단에서 지시문을 넣고 → 원위치
리셋 → 재개한다 (`reset_on_start`). 지시문 없이 도는 래퍼는 "do the task" 로 돌아 팔이 한 번도 안
쉬는데(2026-10-31 117 실측: 1.5초 창 179개 중 쉼 0), 쉼을 기다리면 "지시문이 없어 안 쉬고, 안 쉬어서 못
넣는" 교착이 된다. `reset_on_start` 를 끄면 예전처럼 쉼을 기다려 지시문만 넣는다. 판이 비어 있으면
리셋하지 않는다 — 놓을 사과가 생길 때까지 추론은 그대로다.

**목표 위치도 같이 보낸다** (0.1.6, feature/target-conditioning.md). 지시문을 정하는 순간 집을 사과 하나를 골라
그 중심 `(u, v)`(위 카메라 프레임의 폭·높이로 나눈 0~1)를 버스 `target` 으로 — 지시문보다 먼저 보내서 정책이
새 문장을 읽는 순간 목표도 새것이다. 목표 입력으로 학습한 체크포인트(`<데이터셋>_target`)만 이 값을 쓰고, 다른
체크포인트는 래퍼가 무시한다. 학습 목표는 `tools/targets.py` 가 같은 검출·같은 판 위 판정(`board_apples`)으로
만든다 — 학습과 추론의 목표가 같은 정의여야 한다. 같은 종류가 같은 쪽에 둘이면 점수가 높은 쪽이다 — 목표를
받은 정책은 받은 자리를 집는다. 쉼 창의 프레임마다 그 근처에 보인 중심의 중앙값이다(`_pick_target`).

**목표는 사과를 따라간다** (0.1.7, `_follow_target`). 목표 입력 정책은 사과가 아니라 받은 좌표로 간다 — 프로브에서
목표만 사과 없는 자리로 80mm 옮기면 말단이 70~78mm 따라갔고, 117 에서는 집다가 밀린 사과를 두고 옛 자리를 계속
집었다(2026-11-02). 학습 목표는 에피소드 시작 자리에 고정인데 시연에서는 사과가 집기 전에 거의 안 밀렸다(280개,
상자 중심 p99 16px·최대 21px) — 좌표와 사과가 늘 같은 자리여서, 둘이 어긋날 때 무엇을 믿을지 배운 적이 없다.
그래서 고른 뒤에도 프레임마다 그 사과를 다시 찾아, 20px 넘게 옮겨져 멈췄으면 목표를 그 자리로 옮긴다. 쥔 동안,
팔이 가려 그 종류가 덜 보일 때, 상자 모양이 바뀌었을 때(그리퍼가 덮으면 중심이 반대쪽으로 쏠린다), 이웃과
헷갈릴 때는 안 옮긴다 — 시연 280개에 같은 규칙을 돌리면 한 번도 안 옮긴다. `follow_px` 보다 멀리 밀렸으면 따라가지
않는다. 대신 지시문 어긋남(`_watch_task`)이 "그 쪽에 그 종류가 있나" 가 아니라 "목표 자리 근처에 있나" 를 봐서
멈추고 다시 고른다.

**위 카메라 보정** (0.1.9, `_calibrate_view`). 목표 입력 정책은 화면 좌표를 학습 녹화 때 자리로 읽는다 — 117 위
카메라는 녹화(10-29) 뒤 화면이 38px 아래로 비켜나(≈41mm) 사과 중심을 줘도 그만큼 어긋난 곳으로 내려갔다(2026-11-02).
그래서 켤 때 판 모서리 AprilTag(36h11) 둘을 찾아 지금 화면 → 학습 화면 닮음변환을 재고(`view_ref.json` — 학습
데이터셋에서 `tools/view_ref.py` 로 잰 표지 자리), 검출한 사과 중심을 학습 화면 좌표로 옮긴 뒤 ROI·팔 경계·목표를 다
그 좌표에서 정한다. 117 실측: 표지 모서리 잔차 0.6px, 표지에서 300px 위 커튼 경계도 2px 안. 판이 로봇에 대해 그대로라는
전제다(원위치 팔도 37px 같이 밀려 있었다). 정책이 보는 이미지는 그대로 비켜나 있다 — 목표만 맞춘다.

**정책 이미지도 편다** (0.1.10, `view_image`). 목표만 맞추면 이미지와 목표가 서로 어긋난다 — 117 에서 판 맨 앞 줄
사과는 정책 이미지 속 자리가 학습 때 사과가 한 번도 없던 띠(학습 최저 v 0.749, 그 사과 0.80)라 왼팔이 네 번 연속
출발하지 않았고, 안쪽으로 옮기면 움직였다(2026-11-02). 그래서 잰 변환을 그대로 버스 `view_warp` 로 보내 wrapper 가
이 카메라 스트림을 읽는 정책 카메라 이미지를 학습 화면으로 편다(wrapper/view_warp.py) — 이미지·목표가 같은 행렬로
같이 학습 화면에 맞는다. 켤 때 지우고 보정이 잡히면 보내고 끝날 때 지운다 — 이 플러그인이 도는 동안만 펴진다.
게이트웨이가 `view_warp` 를 모르면 한 번 알리고 목표만 맞춘다(0.1.9 와 같다). `view_image` 를 끄면 예전처럼 목표만.

**출발 안 하면 다른 사과** (0.1.11, `_watch_start`). 정책이 어떤 자리의 사과에는 원위치에서 아예 떠나지 않는다 — 117 에서
왼쪽 맨 앞 줄 빨강에 네 번 연속(각 60초) 서 있었고, 사과를 옮기면 움직였다(2026-11-02). 타임아웃 둘을 기다리면 사과 하나에
2분이다. 그래서 지시 뒤 그 팔이 원위치에 선 채 `start_s`(8초) 가 지나면 그 사과(종류·학습 좌표)를 "출발 안 함" 으로 적고
추론을 멈춘 뒤, 다음 판단이 그 사과를 건너뛰고 다른 사과부터 고른다 — 같은 쪽의 같은 종류, 다른 팔, 다음 종류 순(`_choose`).
남은 것이 그런 사과뿐이면 한 번 더 시키고(`stuck_max`, 출발은 확률이다), 그래도 안 떠나면 추론을 쉬게 하고 판 가운데 쪽으로
옮겨 달라고 기다린다 — 그 자리에서 사과가 사라지면 기록을 지우고 이어 간다(`_forget_moved`). 시계는 원위치에 닿은 때부터
잰다(리셋 파킹은 안 센다). 한 번 떠났으면 그 지시는 더 안 본다 — 집다 멈춘 것은 어긋남·타임아웃 몫이다. 회차 타임아웃
실패(`max_fail`)에는 안 센다. 저널: 기록 `no_start`, 판단 `stuck`·`retry`.

**회차가 끝나면 목표를 지운다** (0.1.8). 옮긴 사과의 자리는 비었는데 목표가 거기 남으면, 정책은 원위치에서 잠깐
쉬고 다시 빈 자리로 집으러 간다 — 쉼이 안 잡혀 다음 판단도 못 한다(2026-11-02 117, 빨강 둘 중 하나를 옮긴 뒤).
목표가 없으면 정책은 선다(목표 없음 = 가만히) — 다음 판단이 새 목표를 보낸다. 같은 까닭으로 어긋남의 "덜 보이면
가렸을 수 있다" 물러섬은 집는 중 쥐기 전에만 쓴다.

    piper-plugin run plugins/apple-sort --camera top=<위 카메라 스트림> \
        --arm left=<CAN> --arm right=<CAN> --api http://127.0.0.1:8000 [--dry-run]
"""

from __future__ import annotations

import json
import math
import re
import statistics
import time
from collections import Counter, deque
from pathlib import Path

from piper_plugin import Decision, MissionPlugin


def parse_rules(text: str) -> list[tuple[str, str]]:
    """`클래스 = 지시문` 줄들 → `[(클래스, 지시문)]` — 위 줄이 먼저다. `#` 줄은 주석."""
    rules = []
    for line in str(text).splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        label, sep, task = line.partition("=")
        if not sep or not label.strip() or not task.strip():
            raise ValueError(f"규칙 줄은 `클래스 = 지시문` 모양이어야 한다: {line!r}")
        rules.append((label.strip(), task.strip()))
    return rules


def parse_roi(text: str) -> tuple[float, float, float, float]:
    parts = [float(v) for v in str(text).split(",")]
    if len(parts) != 4:
        raise ValueError("roi 는 x1,y1,x2,y2 네 값 (0~1)")
    x1, y1, x2, y2 = parts
    return min(x1, x2), min(y1, y2), max(x1, x2), max(y1, y2)


def board_apples(result, shape: tuple[int, int], roi: tuple[float, float, float, float],
                 labels, *, size: bool = False, view=None) -> dict[str, list[tuple[float, ...]]]:
    """검출 결과 → 판 위 사과 `{종류: [(중심 u, 중심 v, 점수)]}` — 중심은 프레임 폭·높이로 나눈 0~1.
    `size` 면 상자 폭·높이(0~1)를 뒤에 붙인다 — 목표 따라가기가 그리퍼에 덮인 상자를 가린다(`_follow_target`).
    `view` 는 지금 화면 px → 학습 화면 px 닮음변환(2×3, `view_transform`) — 위 카메라가 녹화 때 자리에서 비켜났으면
    중심을 학습 화면 좌표로 옮긴 뒤 ROI 를 본다. 라벨러는 학습 프레임 그대로라 안 준다.

    플러그인의 판단과 목표 라벨러(`tools/targets.py`)가 이 함수를 같이 쓴다 — 학습 때 붙인 목표와 추론에서
    보내는 목표가 같은 정의(상자 중심·같은 ROI)여야 정책이 받은 자리를 학습한 대로 읽는다."""
    h, w = shape
    x1, y1, x2, y2 = roi
    scale = 1.0 if view is None else math.hypot(view[0][0], view[1][0])
    out: dict[str, list[tuple[float, float, float]]] = {c: [] for c in labels}
    for (bx1, by1, bx2, by2), k, conf in zip(result.boxes.xyxy.tolist(), result.boxes.cls.int().tolist(),
                                             result.boxes.conf.tolist()):
        label = result.names[int(k)]
        px, py = (bx1 + bx2) / 2, (by1 + by2) / 2
        if view is not None:
            px, py = (view[0][0] * px + view[0][1] * py + view[0][2], view[1][0] * px + view[1][1] * py + view[1][2])
        cx, cy = float(px) / w, float(py) / h
        if label in out and x1 <= cx <= x2 and y1 <= cy <= y2:
            out[label].append((cx, cy, float(conf))
                              + (((bx2 - bx1) * scale / w, (by2 - by1) * scale / h) if size else ()))
    return out


def detect_tags(frame) -> dict:
    """판 AprilTag(36h11, 작업판 인쇄 — scenario/printing.py) → `{id: 모서리 (4, 2) px}`. 태그가 20px 남짓이라
    2배로 키워 찾는다 — 원본 크기로는 학습 프레임에서 둘 중 하나만 잡혔다."""
    import cv2

    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    up = cv2.resize(gray, None, fx=2, fy=2, interpolation=cv2.INTER_CUBIC)
    dic = cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_APRILTAG_36h11)
    corners, ids, _ = cv2.aruco.ArucoDetector(dic, cv2.aruco.DetectorParameters()).detectMarkers(up)
    if ids is None:
        return {}
    return {int(i): c.reshape(4, 2) / 2 for i, c in zip(ids.ravel(), corners)}


def view_transform(live: dict, ref: dict):
    """지금 태그 모서리 → 학습 태그 모서리 닮음변환 — `(2×3 행렬, 배율, 회전°, 잔차 px, 쓴 태그)`. 공통 태그가 둘 미만이면 None
    (태그 하나는 20px 라 배율·회전이 안 선다). 위 카메라만 비켜났다는 전제다 — 판이 로봇에 대해 움직였으면 틀린다."""
    import cv2
    import numpy as np

    common = sorted(set(live) & {int(k) for k in ref})
    if len(common) < 2:
        return None
    src = np.concatenate([np.asarray(live[i], dtype=np.float32) for i in common])
    dst = np.concatenate([np.asarray(ref[str(i)] if str(i) in ref else ref[i], dtype=np.float32) for i in common])
    m, _ = cv2.estimateAffinePartial2D(src, dst, method=cv2.LMEDS)
    if m is None:
        return None
    res = float(np.abs(cv2.transform(src[None], m)[0] - dst).max())
    return m, float(math.hypot(m[0, 0], m[1, 0])), float(math.degrees(math.atan2(m[1, 0], m[0, 0]))), res, common


def resolve_model(name: str, plugin_dir: Path) -> str:
    """경로·HF id 는 그대로. 커스텀 모델 이름(`dualarm_sort_apple-1031-0501`)은 게이트웨이의
    `detector_models_dir` 에서 찾는다 — 설치본(`data/plugins/<이름>`)·저장소 실행(`plugins/<이름>`) 둘 다."""
    p = Path(name).expanduser()
    if p.is_dir():
        return str(p)
    if "/" not in name:
        root = plugin_dir.resolve().parent.parent
        for base in (root / "detector_models", root / "backend" / "data" / "detector_models"):
            if (base / name).is_dir():
                return str(base / name)
    return name


class AppleSort(MissionPlugin):
    poll_s = 0.3
    # "쥐었다" — 그리퍼가 닫힘과 열림 사이에서 `hold_s` 동안 `hold_still` 도 안 움직였다 (`_holding`)
    hold_s = 0.5
    hold_still = 2.0
    # 원위치 — 파킹 목표의 접는 관절만 본다(접히면 위 카메라 시야에서 비킨다). 117 쉼 자세는 J5 가 27~31 이라
    # 전 관절로 재면 허용 8 을 넘는다. 닿은 뒤 `rehold_s` 동안 일시정지를 거듭 보낸다 (`_home_and_hold`)
    home = {"joint2": -100.0, "joint3": 100.0}
    home_tol = 8.0
    rehold_s = 3.0
    # 목표 — 쉼 창의 다른 프레임에서 같은 사과로 볼 거리(px). 사과 지름 ~80px, 검출 중심 흔들림 몇 px
    target_match_px = 25.0
    # 출발 감시 (`_watch_start`) — 떠났다 = 접는 관절이 원위치에서 이만큼 넘게 · 출발 안 한 사과와 같은 사과로 볼 거리(px) ·
    # 한 사과를 시켜 볼 횟수(다른 사과를 다 해 본 뒤 한 번 더). 시연은 지시 뒤 중앙 0.8초·최대 3.3초에 떠났다
    start_leave = 12.0
    stuck_px = 30.0
    stuck_max = 2
    # 목표 따라가기 (`_follow_target`) — 연달아 볼 프레임 수 · 그동안 서 있을 범위(px) · 옮길 최소 거리(px) ·
    # 상자 폭·높이가 고를 때에서 벗어나도 되는 비율 · 두 번째로 가까운 것이 가장 가까운 것의 이 배 안이면 헷갈린다.
    # 시연 280개 실측(2026-11-02): 서 있는 사과는 상자 폭·높이가 99.8% ±15% 안, 그리퍼가 덮어 10px 넘게 쏠린 상자는
    # 117개 중 6개만 ±15% 안이고 그 쏠림이 최대 17px — 20px 이면 시연 어디서도 안 옮긴다(2.5·5Hz 둘 다)
    follow_frames = 3
    follow_still_px = 6.0
    follow_min_px = 20.0
    follow_size = 0.15
    follow_sep = 1.5
    # 화면 보정 (`_calibrate_view`) — 태그 둘이 같이 보인 프레임 몇 장의 모서리 중앙값으로 잰다 · 그만큼 못 모으면 이
    # 시간 뒤 보정 없이 · 받아들일 한계(잔차 px · 배율 · 회전° · 표지 이동 px). 넘으면 태그를 잘못 잡았거나 판이 움직였다.
    # 117 실측(2026-11-02): 잔차 0.6px · 배율 1.017 · 회전 0.09° · 이동 (+2, +38)px, 판 위쪽 끝에서도 2px 안
    view_frames = 3
    view_wait_s = 10.0
    view_max_res = 2.0
    view_scale = (0.9, 1.1)
    view_max_rot = 3.0
    view_max_shift = 120.0

    def setup(self):
        self.rules = parse_rules(self.config["rules"])
        if not self.rules:
            self.stop("규칙이 비었다 — `클래스 = 지시문` 을 한 줄 이상 적어라")
        self.roi = parse_roi(self.config["roi"])
        self.split = float(self.config["arm_split"])
        self.model_path = resolve_model(str(self.config["model"]), self.env.dir)
        self.model_name = Path(self.model_path).name
        self.model = self.detector(self.model_path, device=self.config["device"])
        known = set(self.model.names.values())
        missing = [c for c, _ in self.rules if c not in known]
        if missing:
            self.stop(f"검출 모델에 없는 클래스: {missing} — 모델 클래스: {sorted(known)}")
        self.cam = self.camera("top")
        self.arm_keys = [k for k in ("left", "right") if self.arms.get(k)]
        if not self.arm_keys:
            self.stop("팔 슬롯(left)을 골라라 — 팔이 쉬는지 모르면 지시문을 바꿀 때를 모른다")
        # (시각, 관절 벡터, 그리퍼 최대, 판 위 개수) — `still_s` 창
        self._window: deque = deque()
        self.counts: dict[str, int] = {}
        # 종류 → [(중심 u, 중심 v 0~1, 점수)] — 판 위 개수와 같은 프레임. `{arm}`·목표 위치를 정할 때 쓴다
        self.apples: dict[str, list[tuple[float, float, float]]] = {}
        self._wh = (640, 480)                                   # 마지막 프레임 크기 — 목표 맞추기의 px 거리
        self.task: str | None = None
        self.target: str | None = None
        self.target_arm: str | None = None
        self.target_pos: tuple[float, float] | None = None      # 버스에 보낸 목표 위치 (u, v)
        self._target_warned = False
        # 목표 따라가기 — 고를 때 그 사과 상자(px)·그 쪽에 보인 그 종류 수·고른 자리·연달아 본 중심·옮긴 기록
        self._target_size: tuple[float, float] | None = None
        self._target_n = 0
        self._target_from: tuple[float, float] | None = None
        self._follow: deque = deque()
        self._target_moves: list[list[float]] = []              # [지시 뒤 초, u, v, 옮긴 px]
        self._decided_t = 0.0
        self._following = False                                 # 판단 → 기록 사이(집는 동안)만 따라간다
        # 화면 보정 — 지금 위 카메라 px → 학습 화면 px (2×3). 첫 판단 전에 판 표지로 잰다 (`_calibrate_view`)
        self._view = self._view_inv = self._view_info = self._view_t0 = None
        self._view_seen: list[dict] = []
        self._view_ref = None
        self._view_done = not self.config.get("view_correct", True)
        if self._view_done:
            self.state(view="끔")
        else:
            try:
                self._view_ref = json.loads((self.env.dir / "view_ref.json").read_text())["tags"]
            except (OSError, ValueError, KeyError) as e:
                self.log.warning("화면 보정 기준표(view_ref.json)를 못 읽는다 (%s) — 보정 없이 간다", e)
                self._view_done = True
                self.state(view="기준표 없음 — 보정 없음")
        # 정책 이미지 보정 (`_send_view_warp`) — 지난 실행이 남긴 것을 먼저 지운다. 보정이 잡히면 같은 행렬을 보낸다
        self._image_warned = False
        self._view_sent = False
        self._send_view_warp(None)
        self.state(view_image="잴 때까지 끔" if self.config.get("view_image", True) and not self._view_done else "끔")
        self.fails: dict[tuple[str, str | None], int] = {}       # (종류, 팔) → 연속 타임아웃
        self._fail_board: dict[str, int] | None = None
        self._empty_seen = 0
        self._timed_out = False
        self._skip = False
        self._paused = False                                    # 판이 비어(또는 지시문이 어긋나) 우리가 추론을 쉬게 해 뒀다
        # 지시문 감시 (`_watch_task`) — 대상이 안 보인 시각 · 팔별 최근 그리퍼 (시각, 값) · 지금 쥐었나 · 쥐었다 놓았나 · 어긋나 끝냈나
        self._absent_t0: float | None = None
        self._grips: dict[str, deque] = {}
        self._held = False
        self._released = False
        self._stale = False
        self._look_again = False                                # 어긋나 멈췄다 — 판이 비어 보이면 원위치에서 다시 센다
        # 출발 감시 (`_watch_start`) — 원위치에 선 채 잰 시작 시각 · 이번 지시에 떠났나 · 출발 안 해 끝냈나 ·
        # 출발 안 한 사과 [{label, arm, pos(학습 좌표), n}] — 그 자리의 사과가 옮겨지면 지운다
        self._home_t0: float | None = None
        self._started = False
        self._no_start = False
        self._stuck: list[dict] = []
        self._stuck_said = ""
        # 첫 판단에서 원위치 리셋 후 재개한다 — 이전 실행이 일시정지로 끝났을 수 있다
        self._wake = bool(self.config.get("reset_on_start", True)) and not self.dry_run
        self.state(rules=[c for c, _ in self.rules], model=self.model_name, arms=self.arm_keys)

    # ── 보기 ──

    def _look(self) -> dict[str, int] | None:
        """프레임 한 장 → 판 위 개수. 검출·미리보기를 버스에 올린다."""
        got = self.cam.read_new(timeout_s=1.0)
        if got is None:
            self.state(error="위 카메라 프레임이 안 온다")
            return None
        frame, seq, wall_ns = got
        h, w = frame.shape[:2]
        if not self._view_done:
            self._calibrate_view(frame)
        r = self.model.predict(frame, conf=float(self.config["conf"]), device=self.config["device"], verbose=False)[0]
        r.boxes = self._one_per_apple(r.boxes)
        self.publish_detections(self.name, (h, w), r, seq=seq, wall_ns=wall_ns, model=self.model_name)
        apples = board_apples(r, (h, w), self.roi, [c for c, _ in self.rules], size=True, view=self._view)
        counts = {c: len(v) for c, v in apples.items()}
        self.apples, self._wh = apples, (w, h)
        self._preview(r.plot(), counts)
        return counts

    def _calibrate_view(self, frame) -> None:
        """위 카메라가 학습 녹화 때 자리에서 비켜난 만큼을 판 AprilTag 로 잰다 (머리말 "위 카메라 보정").

        태그 둘이 같이 보인 프레임을 `view_frames` 장 모아 태그마다 모서리 중앙값 → 닮음변환(`view_transform`).
        팔이 태그를 가린 프레임은 건너뛴다. `view_wait_s` 안에 못 모으거나 변환이 한계를 넘으면(태그 오검출·판이
        움직임) 보정 없이 가고 크게 알린다 — 그러면 목표가 학습 때 자리에서 카메라가 비켜난 만큼 어긋난다."""
        import cv2
        import numpy as np

        now = time.monotonic()
        if self._view_t0 is None:
            self._view_t0 = now
        tags = detect_tags(frame)
        known = {int(k) for k in self._view_ref}
        if len(set(tags) & known) >= 2:
            self._view_seen.append(tags)
        if len(self._view_seen) < self.view_frames:
            if now - self._view_t0 >= self.view_wait_s:
                self._view_done = True
                self.log.warning("화면 보정 — %.0f초 동안 판 표지 둘을 같이 못 봤다 (보이는 태그 %s, 기준 %s) → 보정 없이 간다. "
                                 "위 카메라가 학습 녹화 때 자리에서 비켜났으면 목표가 그만큼 어긋난다",
                                 now - self._view_t0, sorted(tags), sorted(known))
                self.state(view="표지 못 찾음 — 보정 없음")
            return
        self._view_done = True
        ids = set.intersection(*(set(t) & known for t in self._view_seen))
        live = {i: np.median(np.stack([t[i] for t in self._view_seen]), axis=0) for i in ids}
        got = view_transform(live, self._view_ref)
        if got is None:
            self.log.warning("화면 보정 — 매 프레임 같이 보인 표지가 둘이 안 된다 (%s) → 보정 없이 간다", sorted(ids))
            self.state(view="표지 모자람 — 보정 없음")
            return
        m, scale, rot, res, used = got
        dx, dy = np.mean([live[i].mean(0) - np.asarray(self._view_ref[str(i)]).mean(0) for i in used], axis=0)
        info = {"shift_px": [round(float(dx), 1), round(float(dy), 1)], "scale": round(1 / scale, 4),
                "rot_deg": round(-rot, 2), "res_px": round(res, 2), "tags": used}
        if (res > self.view_max_res or not self.view_scale[0] <= scale <= self.view_scale[1]
                or abs(rot) > self.view_max_rot or math.hypot(dx, dy) > self.view_max_shift):
            self.log.warning("화면 보정 — 변환이 한계를 넘는다 %s → 보정 없이 간다. 태그를 잘못 잡았거나 판이 로봇에 대해 "
                             "움직였을 수 있다", info)
            self.state(view="한계 넘음 — 보정 없음")
            return
        self._view, self._view_inv, self._view_info = m, cv2.invertAffineTransform(m), info
        self.log.info("화면 보정 — 위 카메라가 학습 녹화 때보다 (%+.0f, %+.0f)px 비켜나 있다 (배율 ×%.3f, 회전 %+.2f°, "
                      "표지 %s, 잔차 %.1fpx) → 사과 자리를 학습 화면 좌표로 옮겨 쓴다", dx, dy, 1 / scale, -rot, used, res)
        self.state(view=f"({dx:+.0f}, {dy:+.0f})px ×{1 / scale:.3f}")
        if self.config.get("view_image", True):
            h, w = frame.shape[:2]
            self._send_view_warp(m, (w, h))
            info["image"] = self._view_sent

    def _to_live(self, u: float, v: float, w: int, h: int) -> tuple[int, int]:
        """학습 화면 좌표(0~1) → 지금 화면 px — 미리보기는 지금 프레임 위에 그린다."""
        x, y = u * w, v * h
        if self._view_inv is not None:
            m = self._view_inv
            x, y = m[0][0] * x + m[0][1] * y + m[0][2], m[1][0] * x + m[1][1] * y + m[1][2]
        return int(round(x)), int(round(y))

    @staticmethod
    def _one_per_apple(boxes):
        """클래스 무관 NMS — 한 사과에 `red_apple`·`small_apple` 상자가 겹쳐 나오면 점수 높은 것만.
        클래스별 NMS 는 이걸 못 지운다(클래스가 다르니까) — 그러면 한 사과가 두 종류로 세어진다."""
        import torch
        import torchvision

        if len(boxes.conf) == 0:
            return boxes
        xyxy, conf, cls = (torch.as_tensor(v) for v in (boxes.xyxy, boxes.conf, boxes.cls))
        keep = torchvision.ops.nms(xyxy.float(), conf.float(), 0.5)
        return type(boxes)(xyxy[keep], conf[keep], cls[keep])

    def _preview(self, canvas, counts: dict[str, int]) -> None:
        import cv2

        import numpy as np

        h, w = canvas.shape[:2]
        # ROI·경계·목표는 학습 화면 좌표다 — 화면 보정을 거꾸로 돌려 지금 프레임에 그린다
        x1, y1, x2, y2 = self.roi
        roi = [self._to_live(u, v, w, h) for u, v in ((x1, y1), (x2, y1), (x2, y2), (x1, y2))]
        cv2.polylines(canvas, [np.int32(roi)], True, (0, 200, 0), 1)
        # 왼팔·오른팔 가르는 선 — 판 E|F 경계에 맞는지 화면에서 본다
        cv2.line(canvas, self._to_live(self.split, y1, w, h), self._to_live(self.split, y2, w, h), (0, 200, 0), 1)
        # 보낸 목표 위치 — 정책이 집으러 갈 자리
        if self.target_pos is not None:
            c = self._to_live(*self.target_pos, w, h)
            if self._target_moves and self._target_from is not None:   # 따라 옮겼다 — 처음 고른 자리와 선으로
                o = self._to_live(*self._target_from, w, h)
                cv2.drawMarker(canvas, o, (170, 170, 170), cv2.MARKER_TILTED_CROSS, 10, 1, cv2.LINE_AA)
                cv2.line(canvas, o, c, (170, 170, 170), 1, cv2.LINE_AA)
            cv2.circle(canvas, c, 16, (255, 0, 255), 2, cv2.LINE_AA)
            cv2.drawMarker(canvas, c, (255, 0, 255), cv2.MARKER_CROSS, 10, 1, cv2.LINE_AA)
        lines = [" ".join(f"{k}:{v}" for k, v in counts.items()), f"task: {self.task or '-'}"]
        if self._view_info:
            dx, dy = self._view_info["shift_px"]
            lines.insert(0, f"view: {dx:+.0f},{dy:+.0f}px x{self._view_info['scale']:.3f}")
        for i, text in enumerate(lines):
            org = (8, h - 12 - 20 * (len(lines) - 1 - i))
            cv2.putText(canvas, text, org, cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 0, 0), 3, cv2.LINE_AA)
            cv2.putText(canvas, text, org, cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 255, 255), 1, cv2.LINE_AA)
        self.publish_preview(f"det_{self.name}", canvas)

    def _arm_pose(self) -> tuple[list[float], float] | None:
        """(관절 벡터, 그리퍼 최대) — 팔 하나라도 상태가 없거나 낡았으면 None (모르면 쉰다고 보지 않는다)."""
        joints: list[float] = []
        grip = 0.0
        for k in self.arm_keys:
            s = self.arm(k).read()
            if s is None or not s.fresh(1.0):
                return None
            joints += [v for n, v in sorted(s.joints.items()) if n != "gripper"]
            grip = max(grip, s.gripper)
        return joints, grip

    def _resting(self, *, first: bool = False) -> bool:
        """한 번 보고 창에 넣는다. 창이 `still_s` 를 덮고, 그동안 관절 변동 < `still_tol`,
        그리퍼 닫힘, 판 위 개수 불변이면 쉰다.

        `first` — 시작 직후의 첫 판단은 **판 위 개수가 안정적이기만 하면** 통과한다. 지시문 없이(또는 학습에 없는
        문장으로) 도는 추론은 팔이 한 번도 안 쉬어서, 쉼을 기다리면 "지시문이 없어 안 쉬고, 안 쉬어서 못 넣는"
        교착이 된다. 시작 직후에는 지시문을 넣고 원위치로 돌리므로(`_start_up`) 팔 쉼을 볼 이유가 없다."""
        counts = self._look()
        if not self._view_done:                                 # 첫 판단은 화면 보정 뒤 — 목표를 학습 화면 좌표로 보낸다
            self._window.clear()
            self.state(resting=False, rest_why="화면 보정 — 판 표지 찾는 중")
            return False
        arms = self._arm_pose()
        now = time.monotonic()
        if counts is None or (arms is None and not first):
            self._window.clear()
            # 지시문은 첫 판단에서야 올라간다 — 쉼이 안 잡히면 화면에 "—" 로 남으니 이유를 같이 올린다
            self.state(resting=False, rest_why="검출 결과가 없다 (카메라 프레임·검출 모델)" if counts is None
                       else "팔 상태가 없거나 낡았다 (robotd·shm)")
            return False
        if arms is None:                                        # first — 팔을 못 읽어도 개수만 본다
            arms = ([], 0.0)
        self.counts = counts
        self._watch_task(now)
        self._window.append((now, arms[0], arms[1], counts, self.apples))
        span = float(self.config["still_s"])
        while len(self._window) > 1 and now - self._window[1][0] >= span:
            self._window.popleft()
        tol = float(self.config["still_tol"])
        grip_max = float(self.config["grip_closed"])
        seen = now - self._window[0][0]
        same = all(c == counts for _, _, _, c, _ in self._window)
        grip = max(g for _, _, g, _, _ in self._window)
        spread = max((max(col) - min(col) for col in zip(*(j for _, j, _, _, _ in self._window))), default=0.0)
        still = grip <= grip_max and spread < tol
        rest = seen >= span and same and still
        ready = seen >= span and same and (still or first)
        if rest:
            why = ""
        elif seen < span:
            why = f"지켜보는 중 ({seen:.1f}/{span:g}초)"
        elif not same:
            kinds = {tuple(sorted(c.items())) for _, _, _, c, _ in self._window}
            why = f"판 위 개수가 바뀐다 ({len(kinds)}가지)"
        elif first:
            why = "시작 직후라 팔 쉼을 기다리지 않는다"
        elif grip > grip_max:
            why = f"그리퍼가 열려 있다 ({grip:.0f} > {grip_max:g})"
        else:
            why = f"팔이 움직인다 (관절 변동 {spread:.1f} ≥ {tol:g})"
        self.state(resting=rest, counts=counts, rest_why=why)
        return ready

    def _holding(self, now: float) -> bool:
        """지시문의 팔(규칙에 `{arm}` 이 없으면 아무 팔)이 무언가를 쥐고 있나 — 그리퍼가 `grip_closed` 와
        `grip_open` 사이에서 `hold_s` 동안 `hold_still` 도 안 움직였다. 사과를 쥐면 그 폭에서 선다(117: 54.9→54.7→54.6).
        열고 닫으며 그 사이를 지나가는 값은 아니다 — 117 에서 0→50→53→59→71 로 1초 걸려 열린 적이 있다.
        상태를 못 읽으면 쥐고 있다고 본다 — 모르면 멈추지 않는 쪽으로 틀린다. 프레임마다 한 번 부른다(값을 쌓는다)."""
        keys = [self.target_arm] if self.target_arm in self.arm_keys else self.arm_keys
        lo, hi = float(self.config["grip_closed"]), float(self.config["grip_open"])
        for k in keys:
            s = self.arm(k).read()
            if s is None or not s.fresh(1.0):
                return True
            hist = self._grips.setdefault(k, deque())
            hist.append((now, s.gripper))
            while len(hist) > 1 and now - hist[1][0] >= self.hold_s:
                hist.popleft()
            vals = [g for _, g in hist]
            if now - hist[0][0] >= self.hold_s and all(lo < g < hi for g in vals) and max(vals) - min(vals) < self.hold_still:
                return True
        return False

    def _watch_reset(self) -> None:
        """새 지시문·새 관찰 — 지난 지시문 동안 쥐고 놓은 것은 그 지시문 몫이다."""
        if self._absent_t0 is not None:                         # 세던 카운트다운이 화면에 남지 않게 (판이 비어 기다릴 때)
            self.state(stale="")
        self._absent_t0 = None
        self._grips.clear()
        self._held = self._released = False
        self._home_t0 = None
        self._started = False

    def _arm_home_dist(self, arm: str | None) -> float | None:
        """이 팔(`None` 이면 모든 팔 중 가장 먼 것)이 원위치에서 얼마나 떨어졌나 — 접는 관절 기준. 못 읽으면 None."""
        keys = [arm] if arm in self.arm_keys else self.arm_keys
        dist = 0.0
        for k in keys:
            s = self.arm(k).read()
            if s is None or not s.fresh(1.0):
                return None
            dist = max(dist, *(abs(s.joints.get(j, v + 1e9) - v) for j, v in self.home.items()))
        return dist

    def _watch_start(self, now: float) -> bool:
        """지시문의 팔이 출발했나 (머리말 "출발 안 하면 다른 사과"). 원위치에 선 채 `start_s` 가 지나면 그 사과를
        `_stuck` 에 적고 추론을 멈춘 뒤 `_wake` 를 세운다 — 다음 판단이 다른 사과부터 골라 리셋하고 다시 시작한다.
        끝냈으면 True. 시계는 원위치에 닿은 때부터 잰다 — 리셋 파킹으로 돌아오는 중에는 안 잰다. 한 번 떠났으면 이번
        지시는 더 안 본다(집다 멈춘 것은 어긋남·타임아웃 몫이다). 드라이런은 팔을 안 움직이므로 안 본다."""
        limit = float(self.config.get("start_s") or 0)
        if limit <= 0 or self.dry_run or self._started or not self._following:
            return False
        d = self._arm_home_dist(self.target_arm)
        if d is None:                                           # 모르면 출발 안 했다고 단정하지 않는다
            return False
        if d > self.start_leave:
            self._started, self._home_t0 = True, None
            self.state(start="")
            return False
        if d > self.home_tol:                                   # 원위치로 가는 중 — 닿은 뒤부터 잰다
            return False
        if self._home_t0 is None:
            self._home_t0 = now
        waited = now - self._home_t0
        where = f"{self.target}({self.target_arm or '-'})"
        if waited < limit:
            self.state(start=f"{where} 출발 기다림 {waited:.0f}/{limit:g}초")
            return False
        pos = self.target_pos
        entry = self._stuck_at(self.target, pos) if pos is not None else None
        if entry is None and pos is not None:
            entry = {"label": self.target, "arm": self.target_arm, "pos": [round(pos[0], 4), round(pos[1], 4)], "n": 0}
            self._stuck.append(entry)
        if entry is not None:
            entry["n"] += 1
        self.log.warning("출발 안 함 — %s 이(가) 목표 %s 에서 %.0f초째 원위치에 서 있다 (이 사과 %s번째) → 추론을 멈추고 "
                         "다른 사과부터 한다", where, entry["pos"] if entry else "없음", waited, entry["n"] if entry else "-")
        self._no_start = True
        self._home_t0 = None
        self._window.clear()
        self.state(start=f"{where} 출발 안 함 — 다른 사과로")
        self._wake = True
        if self.robot.pause().ok:
            self._paused = True
        else:
            self.log.warning("추론 일시정지 실패 — 다음 판단에서 지시문을 넣고 리셋한다")
        return True

    def _stuck_at(self, label: str, c) -> dict | None:
        """이 자리(학습 좌표 중심)의 그 종류가 출발 안 한 사과인가 — 그 기록."""
        w, h = self._wh
        return next((s for s in self._stuck if s["label"] == label
                     and math.hypot((c[0] - s["pos"][0]) * w, (c[1] - s["pos"][1]) * h) < self.stuck_px), None)

    def _blocked(self, label: str, c, retry: bool) -> bool:
        """지금 이 사과를 고르면 안 되나 — 출발 안 한 사과다. 두 번째 바퀴(`retry`, 다른 사과가 없을 때)에는
        `stuck_max` 번을 못 채운 것만 다시 시킨다."""
        s = self._stuck_at(label, c)
        return s is not None and (not retry or s["n"] >= self.stuck_max)

    def _watch_task(self, now: float) -> None:
        """지시문이 판과 맞나 — 쉼과 상관없이 매 프레임 (머리말 "예외"). 그 사과가 옮겨졌으면 목표를 따라 옮기고
        (`_follow_target`), 목표 자리 근처에 없는 채 `stale_s`(쥐었다 놓은 뒤면 `after_release_s`)가 지나면 추론을
        멈추고 `_wake` 를 세운다 — 다음 판단이 시작 직후처럼 팔 쉼 없이 지시문을 다시 넣고 리셋한다."""
        if not self.target or self._wake or self._paused:
            self._watch_reset()
            return
        if self._watch_start(now):
            return
        holding = self._holding(now)
        self._follow_target(now, holding)
        limit = float(self.config.get("stale_s") or 0)
        if limit <= 0:                                          # 어긋남 감시만 끈다 — 쥠 판정 값은 따라가기가 쓴다
            return
        if holding:
            self._held = True
        elif self._held:                                        # 쥐었다 놓았다 — 돌아와 쉴 시간을 더 준다
            self._held, self._released = False, True
        if self._target_seen() or holding:
            if self._absent_t0 is not None:
                self._absent_t0 = None
                self.state(stale="")
            return
        if self._released:
            limit = max(limit, float(self.config.get("after_release_s") or 0))
        if self._absent_t0 is None:
            self._absent_t0 = now
        gone = now - self._absent_t0
        where = f"{self.target}({self.target_arm or '-'})"
        if gone < limit:
            self.state(stale=f"{where} 안 보임 {gone:.1f}/{limit:g}초")
            return
        if self._mine():                                        # 그 쪽에 그 종류는 있다 — 목표 자리에서 멀리 밀렸다
            self.log.warning("지시문이 판과 어긋났다 — %s 이(가) %.1f초째 목표 자리 근처에 없고 그 팔이 쥐고 있지 않다 "
                             "(판 위 %s) → 추론을 멈추고 다시 고른다", where, gone, self.counts)
        else:
            self.log.warning("지시문이 판과 어긋났다 — %s 이(가) %.1f초째 판에 없고 그 팔이 쥐고 있지 않다 (판 위 %s) "
                             "→ 추론을 멈추고 다시 고른다", where, gone, self.counts)
        self._stale = True
        self._absent_t0 = None
        self._window.clear()
        self.state(stale=f"{where} 없음 — 다시 고른다")
        if self.dry_run:                                        # `_start_up` 은 로봇을 부른다 — 드라이런은 쉼을 기다린다
            return
        self._wake = self._look_again = True
        if self.robot.pause().ok:
            self._paused = True
        else:
            self.log.warning("추론 일시정지 실패 — 다음 판단에서 지시문을 넣고 리셋한다")

    def _mine(self) -> list[tuple[float, ...]]:
        """지시문의 종류 중 지시문의 팔 쪽에 보인 것 (규칙에 `{arm}` 이 없으면 전부)."""
        return [a for a in self.apples.get(self.target, [])
                if self.target_arm is None or ("left" if a[0] < self.split else "right") == self.target_arm]

    def _target_seen(self) -> bool:
        """지시문의 사과가 판에 보이나 — 그 팔 쪽의 그 종류가, 목표를 보냈으면 목표에서 `follow_px` 안에.

        종류만 보면 집다가 멀리 밀린 사과도 "판에 있다" 가 된다 — 정책은 빈 목표 자리를 계속 집고, 어긋남은 안
        잡힌다. 집는 중에 아직 쥔 적이 없을 때만은 그 쪽의 그 종류가 고를 때보다 덜 보이면 예전처럼 하나라도 보이면
        있다고 본다 — 그 사과는 다가가는 팔에 가렸을 수 있다(그래서 집기 전 어긋남은 예전과 같은 수, 시연 280개 모의).
        쥐었다 놓은 뒤나 회차 사이에는 물러서지 않는다 — 옮긴 사과의 빈 자리를 같은 종류의 다른 사과가 가려 주면
        정책이 빈 자리를 계속 집어도 못 잡는다(2026-11-02 117: 빨강 둘 중 하나를 옮긴 뒤 왼팔이 빈 자리로 거듭)."""
        mine = self._mine()
        gate = float(self.config.get("follow_px") or 0)
        if not mine or gate <= 0 or self.target_pos is None:
            return bool(mine)
        if len(mine) < self._target_n and self._following and not (self._held or self._released):
            return True
        w, h = self._wh
        tu, tv = self.target_pos
        return any(math.hypot((a[0] - tu) * w, (a[1] - tv) * h) <= gate for a in mine)

    def _follow_target(self, now: float, holding: bool) -> None:
        """고른 사과가 판 위에서 옮겨지면 목표를 그 자리로 옮긴다 (머리말 "목표는 사과를 따라간다").

        집는 동안(판단 → 기록)만 — 회차 사이에 사람이 옛 자리 근처에 새 사과를 놓아도 다음 판단 전에 목표를 안 옮긴다.
        목표에서 `follow_px` 안의 그 종류(그 팔 쪽) 중 가장 가까운 것이 `follow_frames` 장 연달아 `follow_still_px`
        안에 서 있고 목표에서 `follow_min_px` 넘게 떨어졌으면 옮긴다 — 굴러가는 동안은 기다린다. 안 옮기는 때 —
        가림·이웃·든 사과를 옮겨진 사과로 읽지 않으려고:
        - 쥐고 있다(`_holding`) — 든 사과는 팔과 같이 간다. 학습 목표도 집은 자리에 고정이었다
        - 그 쪽의 그 종류가 고를 때보다 적게 보인다 — 팔이 가렸거나 이미 집었다. 남은 이웃을 그 사과로 보지 않는다
        - 상자 폭·높이가 고를 때에서 `follow_size` 넘게 다르다 — 그리퍼가 일부를 덮으면 상자 모양이 바뀌고 중심이
          그리퍼 반대쪽으로 쏠린다(시연에서 12~17px). 그걸 따라가면 목표가 그리퍼에서 달아나고 팔이 쫓는다
        - 두 번째로 가까운 것이 가장 가까운 것의 `follow_sep` 배 안 — 어느 것이 그 사과인지 모른다"""
        gate = float(self.config.get("follow_px") or 0)
        if gate <= 0 or not self._following or self.target_pos is None or self._target_size is None:
            return
        w, h = self._wh
        tu, tv = self.target_pos

        def dist(a) -> float:
            return math.hypot((a[0] - tu) * w, (a[1] - tv) * h)

        mine = sorted(self._mine(), key=dist)
        if holding or not mine or len(mine) < self._target_n:
            self._follow.clear()
            return
        u, v, _, bw, bh = mine[0]
        same_box = (abs(bw * w / self._target_size[0] - 1) <= self.follow_size
                    and abs(bh * h / self._target_size[1] - 1) <= self.follow_size)
        if (dist(mine[0]) > gate or (len(mine) > 1 and dist(mine[1]) < self.follow_sep * dist(mine[0]))
                or not same_box):
            self._follow.clear()
            return
        self._follow.append((u, v))
        while len(self._follow) > self.follow_frames:
            self._follow.popleft()
        if len(self._follow) < self.follow_frames:
            return
        mu = statistics.median(p[0] for p in self._follow)
        mv = statistics.median(p[1] for p in self._follow)
        spread = max(math.hypot((p[0] - mu) * w, (p[1] - mv) * h) for p in self._follow)
        moved = math.hypot((mu - tu) * w, (mv - tv) * h)
        if spread > self.follow_still_px or moved < self.follow_min_px:
            return
        self._follow.clear()
        self.target_pos = (mu, mv)
        self._target_moves.append([round(now - self._decided_t, 1), round(mu, 4), round(mv, 4), round(moved)])
        self.log.info("목표를 따라 옮긴다 — %s 이(가) %.0fpx 옮겨졌다 → (%.3f, %.3f)", self.target, moved, mu, mv)
        self.state(follow=f"{len(self._target_moves)}번 옮김 · 최근 {moved:.0f}px")
        self._send_target(self.target_pos)

    def _side_votes(self, label: str) -> Counter:
        """쉼 창의 프레임마다 이 종류가 판 어느 쪽에 있었나 — 쪽별 프레임 수.

        개수는 창 전체로 확인하는데 쪽을 마지막 한 프레임으로 정하면, 점수가 낮은 모델(참 점수 0.1 안팎)의
        한 프레임 흔들림이 그대로 지시문이 된다 — .117 에서 왼쪽 사과에 right 가 나갔다(2026-11-01)."""
        return Counter(side for *_, apples in self._window
                       for side in {"left" if cx < self.split else "right" for cx, *_ in apples.get(label, [])})

    def _pick_target(self, label: str, arm: str | None, retry: bool = False
                     ) -> tuple[tuple[float, float], tuple[float, float], int] | None:
        """집을 사과 하나 — 중심 `(u, v)`(목표 입력 체크포인트에 보낸다, 머리말 "목표 위치")·상자 `(폭, 높이)` px·
        그 쪽에 보인 그 종류 수. 상자와 개수는 목표 따라가기(`_follow_target`)의 기준이다.

        그 쪽(팔)에 그 종류가 보인 가장 최근 프레임에서 점수가 가장 높은 것을 고르고, 쉼 창의 프레임마다 그 근처
        (`target_match_px`)에 보인 같은 종류의 중심을 모아 중앙값을 낸다 — 점수가 낮은 모델의 한 프레임 흔들림을
        목표로 보내지 않는다. 마지막 프레임만 보면 그 한 장에서 사과가 빠졌을 때 목표 없음이 나간다(팔 쪽은 창의
        과반으로 정했다 — `_side_votes`). 창 전체에서 그 쪽에 안 보였으면 None(목표 없음).

        출발 안 한 사과(`_blocked`)는 고르지 않는다 — 그 쪽에 그것뿐이면 None 이고, 판단이 다음 후보로 넘어간다."""
        w, h = self._wh

        def on_side(cx: float) -> bool:
            return arm is None or ("left" if cx < self.split else "right") == arm

        def ok(c) -> bool:
            return on_side(c[0]) and not self._blocked(label, c, retry)

        frames = [apples for *_, apples in self._window] or [self.apples]
        ref = next((a for a in reversed(frames) if any(ok(c) for c in a.get(label, []))), None)
        if ref is None:
            return None
        best = max((c for c in ref[label] if ok(c)), key=lambda c: c[2])
        cx, cy = best[0], best[1]
        n = sum(1 for c in ref[label] if on_side(c[0]))

        def dist(c) -> float:
            return math.hypot((c[0] - cx) * w, (c[1] - cy) * h)

        seen = []
        for apples in frames:
            near = [c for c in apples.get(label, []) if dist(c) < self.target_match_px]
            if near:
                seen.append(min(near, key=dist))
        seen = seen or [best]
        return ((statistics.median(c[0] for c in seen), statistics.median(c[1] for c in seen)),
                (statistics.median(c[3] for c in seen) * w, statistics.median(c[4] for c in seen) * h), n)

    def _send_target(self, pos: tuple[float, float] | None) -> None:
        """목표 위치를 버스로 (`None` = 지움). 못 보내도 지시문은 그대로 간다 — 목표 입력으로 학습하지 않은
        체크포인트에는 상관없는 값이다. 게이트웨이가 `target` 을 모르면(v0.5.25 이하) 한 번만 알린다."""
        self.state(target_pos="" if pos is None else f"{pos[0]:.3f},{pos[1]:.3f}")
        if self.dry_run:
            self.log.info("[dry_run] target: %s", pos)
            return
        r = self.robot.set_params(target=None if pos is None else [round(pos[0], 4), round(pos[1], 4)])
        applied = (r.json() or {}).get("applied", {}) if r.ok else {}
        if r.ok and isinstance(applied, dict) and "target" in applied:
            return
        if not self._target_warned:
            self._target_warned = True
            self.log.warning("게이트웨이가 목표 위치를 받지 않았다 (%s) — target 을 모르는 게이트웨이다. "
                             "목표 입력으로 학습한 체크포인트면 목표 없이 돈다", r.status)

    def _send_view_warp(self, m, wh: tuple[int, int] | None = None) -> None:
        """정책 이미지 보정을 버스 `view_warp` 로 (`None` = 끔, 머리말 "정책 이미지도 편다").

        `m` 은 목표에 쓰는 것과 같은 지금 화면 px → 학습 화면 px 행렬이고, `wh` 는 그것을 잰 프레임 크기다. wrapper 가
        이 카메라 스트림을 읽는 정책 카메라 이미지를 이 행렬로 편다. 게이트웨이가 `view_warp` 를 모르면(v0.5.26 이하)
        켤 때만 한 번 알린다 — 그러면 목표만 맞는다."""
        if m is None and not self._view_sent and self._image_warned:
            return
        if self.dry_run:
            self.log.info("[dry_run] view_warp: %s", None if m is None else [[round(float(x), 4) for x in row] for row in m])
            self._view_sent = m is not None
            return
        value = None if m is None else {"stream": self.cam.stream, "frame": [int(wh[0]), int(wh[1])],
                                        "m": [[round(float(x), 6) for x in row] for row in m]}
        r = self.robot.set_params(view_warp=value)
        applied = (r.json() or {}).get("applied", {}) if r.ok else {}
        ok = r.ok and isinstance(applied, dict) and "view_warp" in applied
        if m is None:
            self._view_sent = False
            return
        self._view_sent = ok
        if ok:
            self.state(view_image="켬")
            self.log.info("정책 이미지 보정을 켰다 — 정책이 보는 위 카메라(%s) 이미지도 같은 변환으로 학습 화면에 맞춘다",
                          self.cam.stream)
        elif not self._image_warned:
            self._image_warned = True
            self.state(view_image="게이트웨이 미지원")
            self.log.warning("게이트웨이가 정책 이미지 보정(view_warp)을 받지 않았다 (%s) — view_warp 를 모르는 게이트웨이다. "
                             "목표만 학습 화면 좌표로 보낸다", r.status)

    def teardown(self) -> None:
        """끝날 때 정책 이미지 보정을 지운다 — 이 플러그인이 도는 동안만 편다. setup 이 도중에 멈췄어도 불린다."""
        if getattr(self, "_view_sent", False):
            self._send_view_warp(None)

    def _arm_order(self, label: str, rule: str) -> list[str | None]:
        """이 종류를 집을 팔 — 먼저 시킬 것부터. 규칙에 `{arm}` 이 없으면 `[None]`(팔을 안 밝힌다).

        쪽은 쉼 창 프레임의 과반에서 보인 것만 (`_side_votes`) — 과반이 없으면 가장 많이 보인 쪽.
        사과가 판 양쪽에 있으면 상자 쪽 팔이 먼저다(덜 뻗는다 — 건너가 놓기가 데이터에서 가장 흩어졌다).
        상자가 가운데(위)면 많이 보인 쪽. 연속 실패가 `max_fail` 에 닿은 쪽은 뺀다."""
        if "{arm}" not in rule:
            sides: list[str | None] = [None]
        else:
            votes = self._side_votes(label)
            sides = ([s for s, v in votes.most_common() if v * 2 > len(self._window)]
                     or [s for s, _ in votes.most_common(1)])
            box = re.search(r"\b(left|right) box\b", rule)
            if box and box.group(1) in sides:
                sides.remove(box.group(1))
                sides.insert(0, box.group(1))
        limit = int(self.config["max_fail"])
        return [s for s in sides if self.fails.get((label, s), 0) < limit]

    # ── 훅 ──

    def observe(self) -> dict:
        self._window.clear()
        # 끝난 회차에서 든 것은 그 회차 몫이다 — 끝난 뒤에도 옛 지시문으로 계속 움직이면 여기서 잡는다
        self._watch_reset()
        settled = self.wait_until(lambda: self._resting(first=self._wake), float(self.config["episode_timeout_s"]))
        return {"counts": dict(self.counts) if settled else None}

    def decide(self, ctx) -> Decision:
        self._stale = False                                     # observe 중에 어긋났으면 여기서 다시 고르는 것으로 끝
        board = ctx["counts"]
        if board is None:
            return Decision.skip("not_resting")                 # 팔이 안 쉰다 — 다음 회차에 다시 본다
        if board != self._fail_board:                           # 판이 바뀌었다 — 실패 기록은 옛 판 것
            self.fails.clear()
        if not any(board.values()):
            if self._look_again:                                # 멈춘 팔이 사과를 가렸을 수 있다 — 비켜서 다시 센다
                self._look_again = False
                self._home_and_hold()
                return Decision.skip("board_empty_recheck")
            self._empty_seen += 1
            if self._empty_seen == 2:                           # 두 번 연속 비었을 때만 — 한 번은 오검출일 수 있다
                self._wrap_up("판 위 사과를 모두 옮겼다", wait=self.config.get("on_empty_plugin") == "wait")
            return Decision.skip("board_empty")
        self._empty_seen = 0
        self._forget_moved()
        choice = self._choose(board)
        if choice is None:
            if self._stuck:                                     # 남은 것이 출발 안 한 사과뿐 — 사람이 옮기기를 기다린다
                return self._wait_stuck()
            # 사람이 볼 차례 — 기다려도 풀리지 않으니 `on_empty_plugin` 과 상관없이 끝낸다
            self._wrap_up(f"남은 사과가 있는데 모든 종류·팔이 연속 {self.config['max_fail']}번 실패했다 — 사람이 볼 차례",
                          wait=False)
        label, rule, arm, picked, retry = choice
        self._stuck_said = ""
        self.state(waiting="", start="")
        task = rule.replace("{arm}", arm) if arm else rule
        if task != self.task:
            self.log.info("지시문 → %r (판 위 %s)", task, board)
        self.task, self.target, self.target_arm = task, label, arm
        self._watch_reset()
        self.state(task=task, arm=arm or "", stale="", follow="")
        # 목표가 지시문보다 먼저다 — 정책이 새 문장을 읽는 순간 목표도 새것이어야 한다
        self.target_pos, self._target_size, self._target_n = picked or (None, None, 0)
        self._target_from, self._target_moves, self._decided_t = self.target_pos, [], time.monotonic()
        self._follow.clear()
        self._following = True
        self._send_target(self.target_pos)
        if self._wake:
            self._start_up(task)
        elif self._paused:
            self._resume_with(task)
        # 팔을 고른 근거 — 쪽별 프레임 수(창 크기 중)와 마지막 프레임의 사과 중심 x
        seen = {**self._side_votes(label), "frames": len(self._window),
                "x": [round(cx, 2) for cx, *_ in self.apples.get(label, [])]}
        pos = None if self.target_pos is None else [round(c, 4) for c in self.target_pos]
        extra = {"retry": True} if retry else {}
        if self._stuck:                                         # 미뤄 둔 사과 — 출발 안 한 자리와 횟수
            extra["stuck"] = [[s["label"], s["arm"], s["pos"], s["n"]] for s in self._stuck]
        return Decision.task(task, target=label, arm=arm, counts=board, seen=seen, target_pos=pos, view=self._view_info,
                             **extra)

    def _choose(self, board: dict[str, int]) -> tuple | None:
        """이번에 시킬 (종류, 규칙, 팔, 고른 사과, 다시 시키기) — 규칙 순서·팔 순서(`_arm_order`)대로 첫 후보.

        출발 안 한 사과(`_stuck`)는 건너뛴다 — 그 쪽의 그 종류가 그것뿐이면 다른 팔, 다른 종류로 넘어간다. 그것밖에
        없으면 두 번째 바퀴에 `stuck_max` 번을 못 채운 것을 다시 시킨다(출발은 확률이라 판이 바뀌면 떠날 수 있다).
        그래도 없으면 None."""
        for retry in (False, True):
            for label, rule in self.rules:
                for arm in (self._arm_order(label, rule) if board.get(label, 0) > 0 else []):
                    picked = self._pick_target(label, arm, retry)
                    if picked is None and self._only_blocked(label, arm, retry):
                        continue
                    return label, rule, arm, picked, retry
            if not self._stuck:
                break
        return None

    def _only_blocked(self, label: str, arm: str | None, retry: bool) -> bool:
        """그 쪽의 그 종류가 지금 고를 수 없는 출발 안 한 사과뿐인가 (`_pick_target` 이 None 일 때 — 아예 안 보인
        것과 가른다. 안 보였으면 예전처럼 목표 없이 시킨다)."""
        frames = [apples for *_, apples in self._window] or [self.apples]
        return any(self._blocked(label, c, retry) for a in frames for c in a.get(label, [])
                   if arm is None or ("left" if c[0] < self.split else "right") == arm)

    def _forget_moved(self) -> None:
        """출발 안 한 사과 중 그 자리에 이제 그 종류가 없는 것을 지운다 — 사람이 옮겼거나 다른 팔이 밀었다. 판단은 팔이
        쉴 때라 가림은 없다."""
        frames = [apples for *_, apples in self._window] or [self.apples]
        w, h = self._wh

        def still_there(s: dict) -> bool:
            return any(math.hypot((c[0] - s["pos"][0]) * w, (c[1] - s["pos"][1]) * h) < self.stuck_px
                       for a in frames for c in a.get(s["label"], []))

        there = [s for s in self._stuck if still_there(s)]
        gone = [s for s in self._stuck if not still_there(s)]
        if gone:
            self.log.info("출발 안 한 사과가 그 자리에 없다 — 옮겨졌다고 보고 지운다: %s",
                          ", ".join(f"{s['label']}({s['arm'] or '-'}) {s['pos']}" for s in gone))
            self._stuck = there

    def _wait_stuck(self) -> Decision:
        """남은 사과가 다 출발 안 한 사과다(사과마다 `stuck_max` 번) — 추론을 쉬게 하고 사람이 옮기기를 기다린다.
        옮기면 그 기록이 지워져(`_forget_moved`) 다음 판단이 이어 간다."""
        kinds = ", ".join(f"{s['label']}({s['arm'] or '-'})" for s in self._stuck)
        msg = f"남은 사과가 팔이 출발하지 않는 자리에 있다: {kinds} → 판 가운데 쪽으로 옮겨 주세요"
        if msg != self._stuck_said:
            self._stuck_said = msg
            self.log.warning("%s (자리 %s, 사과마다 %d번 시켜 봤다)", msg, [s["pos"] for s in self._stuck], self.stuck_max)
        if not self.dry_run and not self._paused:
            if self.robot.pause().ok:
                self._paused = True
            else:
                self.log.warning("추론 일시정지 실패")
        self.state(waiting=msg)
        return Decision.skip("stuck_only")

    def done(self, ctx) -> bool:
        if self._skip:                                          # [skip] 버튼
            self._skip = False
            return True
        rest = self._resting()
        if self._stale or self._no_start:                       # 어긋났다·출발 안 했다 — 추론은 이미 멈췄다
            return True
        return rest and self.counts != ctx["counts"]

    def record(self, ctx, result) -> dict:
        before, after = ctx["counts"], dict(self.counts)
        self._timed_out = bool(result.get("timeout"))
        self._following = False
        rec = {"target": self.target, "arm": self.target_arm, "task": self.task, "counts_before": before}
        if self._target_moves:                                  # 따라 옮긴 목표 — 처음 자리는 판단의 target_pos
            rec["target_moves"] = list(self._target_moves)
        if self._no_start:                                      # 팔이 원위치를 안 떠나 끝냈다 — 그 사과는 `_stuck` 에 적혔다
            self._no_start = False
            rec["no_start"] = True
        if result.get("done"):
            delta = {k: after.get(k, 0) - before.get(k, 0) for k in before}
            rec.update(counts_after=after, delta=delta,
                       picked_target=delta.get(self.target) == -1
                       and all(v == 0 for k, v in delta.items() if k != self.target))
            if self._stale:                                     # 쉬어서가 아니라 어긋나서 끝났다 — 옮긴 뒤 헤맸을 수도 있다
                self._stale = False
                rec["stale"] = True
            elif not rec["picked_target"] and not rec.get("no_start"):
                self.log.warning("판이 지시와 다르게 바뀌었다: %s (목표 %s)", delta, self.target)
        elif self._timed_out and self.target:
            key = (self.target, self.target_arm)
            self.fails[key] = self.fails.get(key, 0) + 1
            self._fail_board = before
            self.log.warning("%s(%s) 타임아웃 (%d번째 연속)", self.target, self.target_arm or "-", self.fails[key])
        if result.get("done") and self.target_pos is not None:
            # 끝난 회차의 목표는 대개 빈 자리다 — 남겨 두면 정책이 원위치에서 잠깐 쉬고 다시 그 자리로 집으러 간다
            # (좌표를 믿는다, 2026-11-02 117 회차 7→8: 쉼이 안 잡혀 다음 판단도 못 했다). 지우면 다음 판단까지 선다
            # — 목표 없음 = 가만히(프로브 원위치 2.9mm). 다음 판단이 새 목표를 지시문보다 먼저 보낸다
            self.target_pos = None
            self._send_target(None)
        return rec

    def reset(self) -> None:
        if self._timed_out and self.config.get("reset_on_timeout") and not self.dry_run:
            r = self.robot.reset()
            self.log.info("타임아웃 — 리셋 (%s)", r.status)

    def on_command(self, name, args):
        if name == "skip":
            self._skip = True

    def _wrap_up(self, reason: str, *, wait: bool) -> None:
        """판이 비었거나 더 할 수 없을 때 — 추론은 `on_empty_inference` 대로 두고, 플러그인은 기다리거나 끝낸다.

        끝낼 때 task 를 공백으로 비우는 건 **우리가 추론을 쉬게 했을 때뿐**이다 — 돌고 있는 정책에
        빈 문장을 넣으면 학습에 없던 입력이라 팔이 무엇을 할지 모른다."""
        act = self.config.get("on_empty_inference")
        if not self.dry_run:
            if act == "pause" and not self._paused:
                if self.robot.pause().ok:
                    self._paused = True
                else:
                    self.log.warning("추론 일시정지 실패")
            elif act == "stop_inference":
                self.robot.stop_inference()
                wait = False                                    # 추론이 없으면 기다릴 것도 없다
        if wait:
            self.log.info("%s — 사과가 다시 올라오기를 기다린다", reason)
            self.state(waiting=reason)
            return
        if self._paused and self.robot.set_task("").ok:
            self.task = None
            self.state(task="")
            self.target_pos = None
            self._send_target(None)
        self.stop(reason)

    def _start_up(self, task: str) -> None:
        """시작 직후 첫 판단 — 지시문을 먼저 넣고, 원위치로 돌린 뒤, 추론을 재개한다.

        지시문이 먼저인 이유: 이전 실행이 task 를 공백으로 비우고 끝냈을 수 있다 — 리셋이 끝나 재개되는
        순간 정책이 읽는 문장이 이것이어야 한다. 리셋은 래퍼가 원점 복귀 뒤 스스로 재개하지만(일시정지와
        무관하게 최상단에서 처리), 일시정지로 끝난 추론을 확실히 깨우도록 재개를 한 번 더 보낸다."""
        if not self.robot.set_task(task).ok:
            self.log.warning("시작 지시문 전달 실패 — 다음 회차에 다시 시도한다")
            return
        r = self.robot.reset()
        if not r.ok:
            self.log.warning("원위치 리셋 실패 (%s) — 재개만 한다", r.status)
        self.robot.resume()
        self._wake = self._paused = self._look_again = False
        self.log.info("시작 — 원위치 리셋 후 추론 재개 (%s)", task)

    def _home_dist(self) -> float:
        """원위치(`home` 의 접는 관절)까지 가장 먼 관절의 거리 — 팔 상태를 못 읽으면 무한대."""
        dist = 0.0
        for k in self.arm_keys:
            s = self.arm(k).read()
            if s is None or not s.fresh(1.0):
                return float("inf")
            dist = max(dist, *(abs(s.joints.get(j, v + 1e9) - v) for j, v in self.home.items()))
        return dist

    def _home_and_hold(self) -> None:
        """어긋나 멈춘 팔을 원위치로 보내고 거기서 다시 멈춘다 — 판이 비어 보이는 게 멈춘 팔이 사과를 가려서일
        수 있다. 그대로 "사과를 기다린다" 로 가면 가린 팔이 안 비키니 영영 못 센다.

        리셋은 파킹을 마치면 래퍼가 스스로 재개하고, 파킹 도중의 일시정지는 먹지 않는다(grasp-guard 머리말) —
        그래서 원위치에 닿은 뒤 `rehold_s` 동안 일시정지를 거듭 보낸다(멱등). 재개와 다음 일시정지 사이(≤0.2초)는
        정책이 옛 지시문으로 한 걸음 낼 수 있다."""
        self.log.info("판이 비어 보인다 — 멈춘 팔이 사과를 가렸을 수 있어 원위치에서 다시 센다")
        if not self.robot.reset().ok:
            self.log.warning("원위치 리셋 실패 — 멈춘 자리에서 센다")
            return
        if not self.wait_until(lambda: self._home_dist() <= self.home_tol, 20.0):
            self.log.warning("20초 안에 원위치에 닿지 않았다 — 일시정지만 해 둔다")
            self.robot.pause()
            return
        end = time.monotonic() + self.rehold_s
        while time.monotonic() < end:
            self.robot.pause()
            self.sleep(0.2)

    def _resume_with(self, task: str) -> None:
        """쉬게 해 둔 추론을 깨운다 — 지시문을 먼저 넣고 깨워서 옛 문장으로 한 걸음도 안 가게 한다."""
        if self.robot.set_task(task).ok and self.robot.resume().ok:
            self._paused = False
            self.log.info("사과가 다시 올라왔다 — 추론 재개")
        else:
            self.log.warning("추론 재개 실패 — 다음 회차에 다시 시도한다")


if __name__ == "__main__":
    raise SystemExit(AppleSort.main())
