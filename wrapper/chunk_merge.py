"""청크 이어 붙이기 — 경계 크로스페이드 + 청크 단위 무지연 스무딩 (로컬 큐 모드).

## 왜

큐 모드는 새 청크가 오면 남은 큐를 **통째로 갈아끼운다.** 팔은 명령보다 늦고(실측 2스텝)
EMA(`lowpass_alpha`)가 그만큼 더 늦추므로, 정책이 보는 상태는 이전 청크가 가 있던 자리보다
뒤다. 새 청크는 그 뒤처진 자리에서 시작한다 — 경계마다 명령이 진행 방향 **반대로** 2~4cm
되돌아갔다(2026-10-28 추론 로그 12개, 경계의 74~91%). 추론이 15ms 여도 같았다 — 지연 탓이
아니다. 말단 떨림의 대부분이 여기서 왔다.

EMA 를 세게 걸면 흐려지긴 하지만 지연이 늘고, 늘어난 지연이 다시 되돌아감을 키운다.

## 무엇을 하나

1. 정렬 — 청크의 k 번째는 관측 스텝 s+k 의 액션이다. 그 사이 이미 보낸 칸은 버린다
   (`temporal_ensemble` 과 같은 규약).
2. 크로스페이드 — 앞 K 칸을 남은 옛 큐에서 새 청크로 코사인 가중(1→0)으로 옮겨 간다.
   옛 큐가 K 보다 짧으면 마지막 값을 늘인다 — 큐가 비면 팔은 실제로 그 값에 서 있다.
3. 스무딩 — [직전에 보낸 값] + 합친 큐 전체에 Savitzky-Golay(2차). 청크는 미래까지 이미
   있으므로 **지연이 없다.** 같은 일을 하는 EMA 와 다른 점이 이것이다.

둘 다 0 이면 지금처럼 교체한다(정렬도 하지 않는다). 이어 붙일 것이 없는 첫 청크(시작·리셋
직후)는 정렬·크로스페이드 없이 **스무딩만** 건다 — 처음엔 그대로 보냈더니 정지 상태에서 출발하는
첫 청크가 정책 원본의 떨림을 그대로 실어 한 스텝에 최대 12.8mm 튀었다(ACT, 2026-10-28).

디버그 기록 3개로 명령을 재구성해 비교했다(스텝당 가속 변화 상위 1%, 말단 mm):
교체+EMA α0.28 이 17~23mm·지연 2~4스텝, 크로스페이드 10+스무딩 7 이 5~8mm·지연 0.
실물(같은 날 22시, 3모델)에서 말단 떨림 30~50%↓·속도 15~35%↑ — 그래서 10/7 이 기본이다.

## 상한을 낮게 둔 이유

크게 잡을수록 떨림은 거의 그대로인데 **모서리를 깎는다.** 크로스페이드 20·창 15 는 10·7 보다
가속 p99 가 8.7→8.0mm 로 조금 낮을 뿐, 정책이 의도한 자리에서 전체 중앙값 최대 25mm·잡는
순간 최대 11mm 벗어났다 — 잡으러 내려가는 짧은 하강이 무뎌진다. 12·9 에서 자른다.

torch 를 import 하지 않는다 — 테스트가 그냥 돈다.
"""

from __future__ import annotations

import numpy as np

POLYORDER = 2
# 2차 다항식은 3점을 그대로 지나간다 — 5점부터 다듬는 효과가 있다
MIN_WINDOW = 5
MAX_WINDOW = 9
MAX_BLEND = 12
# 시작값을 못 받은 경로(화면 밖에서 띄운 추론)도 화면 기본값과 같게 돈다 — inference_params 와 같은 값
DEFAULT_BLEND = 10
DEFAULT_WINDOW = 7


def savgol(x: np.ndarray, window: int) -> np.ndarray:
    """(n, D) 를 시간 축으로 Savitzky-Golay. 가장자리는 가장자리 창의 다항식으로 채운다
    (scipy `mode="interp"` 와 같다). 창이 길이보다 길면 들어가는 가장 큰 홀수로 줄인다."""
    n = len(x)
    w = min(window, n if n % 2 else n - 1)
    if w < MIN_WINDOW:
        return x
    v = np.vander(np.arange(w), POLYORDER + 1, increasing=True)
    hat = v @ np.linalg.pinv(v)       # 행 r = 창 안 r 번째 점의 적합값을 내는 가중치
    h = w // 2
    out = np.empty_like(x)
    for i in range(n):
        lo = min(max(0, i - h), n - w)
        out[i] = hat[i - lo] @ x[lo:lo + w]
    return out


class ChunkMerger:
    """새 청크를 남은 큐에 이어 붙인다. 상태가 없다 — 부르는 쪽이 큐와 직전 액션을 넘긴다."""

    def __init__(self, blend_steps: int = DEFAULT_BLEND, smooth_window: int = DEFAULT_WINDOW) -> None:
        self.blend_steps = blend_steps
        self.smooth_window = smooth_window

    def update_param(self, key: str, value) -> bool:
        """버스 파라미터 업데이트. 처리했으면 True."""
        if key == "chunk_blend_steps":
            self.blend_steps = max(0, min(MAX_BLEND, int(value)))
            return True
        if key == "chunk_smooth_window":
            self.smooth_window = max(0, min(MAX_WINDOW, int(value)))
            return True
        return False

    def window(self) -> int:
        """실제로 쓰는 스무딩 창. 5 미만은 꺼짐(0), 짝수는 다음 홀수 — 가운데가 있어야 지연이 0 이다."""
        if self.smooth_window < MIN_WINDOW:
            return 0
        return self.smooth_window | 1

    @property
    def enabled(self) -> bool:
        return self.blend_steps > 0 or self.window() > 0

    def merge(self, chunk, old, elapsed: int, last_sent=None, keep=()) -> np.ndarray:
        """보낼 큐를 돌려준다 (m, D).

        chunk     — 새 청크 (n, D). 0 번째가 관측 스텝의 액션이다
        old       — 아직 안 보낸 옛 큐 (k, D). 비어 있을 수 있다
        elapsed   — 관측 뒤 이미 보낸 칸 수. 그만큼 새 청크 앞을 버린다
        last_sent — 직전에 보낸 액션 (D,). 스무딩이 여기서 이어진다
        keep      — 손대지 않을 차원 (그리퍼 우회 — `ActionFilter.gripper_bypass_filter`)
        """
        chunk = np.asarray(chunk, dtype=np.float64)
        if not self.enabled or len(chunk) == 0:
            return chunk
        dim = chunk.shape[1]
        old = np.asarray(old, dtype=np.float64).reshape(-1, dim)
        if last_sent is not None:
            last_sent = np.asarray(last_sent, dtype=np.float64).reshape(dim)
        ref = old[-1] if len(old) else last_sent
        if ref is None:
            # 이어 붙일 것이 없다 — 첫 청크. 스무딩만 (모듈 설명 참고)
            win = self.window()
            out = savgol(chunk, win) if win else chunk.copy()
            if len(keep):
                out[:, list(keep)] = chunk[:, list(keep)]
            return out

        new = chunk[min(max(0, elapsed), len(chunk) - 1):]
        out = new.copy()

        k = min(self.blend_steps, len(new))
        if k:
            prev = np.vstack([old, np.repeat(ref[None], max(0, k - len(old)), axis=0)])[:k]
            w = 0.5 * (1 + np.cos(np.pi * np.arange(1, k + 1) / (k + 1)))[:, None]
            out[:k] = w * prev + (1 - w) * new[:k]

        win = self.window()
        if win:
            if last_sent is None:
                out = savgol(out, win)
            else:
                out = savgol(np.vstack([last_sent[None], out]), win)[1:]

        if len(keep):
            idx = list(keep)
            out[:, idx] = new[:, idx]
        return out
