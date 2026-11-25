"""One-Euro filter (Casiez, Roussel, Vogel, CHI 2012), element-wise on a vector stream.

출처: WeGo-Robotics/NERO_DualArm_VR PR #18 `common/one_euro.py` (09075249) — **그대로 옮겼다**
(feature/pika.md §5). 원본은 IK 가 낸 관절값(rad)에 관절마다 건다 — PoseLeader 도 그렇게 쓴다.
기본값은 원본의 `--smooth 2 2` = `min_cutoff` 2Hz · `beta` 2.

A low-pass whose cutoff rises with speed: min_cutoff (Hz) at rest removes tracking and IK jitter,
min_cutoff + beta * |velocity| while moving keeps the lag small.
"""
import math

import numpy as np


def _alpha(dt, cutoff):
    r = 2 * math.pi * cutoff * dt
    return r / (r + 1)


class OneEuro:
    def __init__(self, min_cutoff, beta, d_cutoff=1.0):
        self.min_cutoff, self.beta, self.d_cutoff = min_cutoff, beta, d_cutoff
        self.x = self.dx = self.t = None

    def __call__(self, x, t):
        """x: vector, t: its time in seconds -> filtered vector. A repeated or older t returns
        the last output; the first call passes x through."""
        x = np.asarray(x, float)
        if self.x is None:
            self.x, self.dx, self.t = x.copy(), np.zeros_like(x), t
            return self.x.copy()
        dt = t - self.t
        if dt <= 0:
            return self.x.copy()
        self.dx += _alpha(dt, self.d_cutoff) * ((x - self.x) / dt - self.dx)
        cutoff = self.min_cutoff + self.beta * np.abs(self.dx)
        self.x += np.vectorize(_alpha)(dt, cutoff) * (x - self.x)
        self.t = t
        return self.x.copy()
