"""
액션 필터링 모듈 (lerobot_wrapper / grpc_wrapper 공유).

ZMQ로 실시간 변경 가능한 파라미터:
  - lowpass_alpha: 저역 통과 필터 (1.0=OFF, 0.1=강)
  - max_velocity: 관절 최대 속도 (deg/s, 0=무제한)
  - max_gripper_velocity: 그리퍼 최대 속도 (%/s, 0=무제한)
  - max_jerk: 가속도 변화율 제한 (deg/s², 0=무제한)
  - interpolation_steps: 액션 보간 스텝 수 (0=OFF)
"""


class ActionFilter:
    def __init__(self, fps: float = 20.0):
        self.fps = fps
        self.lowpass_alpha: float = 0.5
        self.max_velocity: float = 180.0
        self.max_gripper_velocity: float = 300.0
        self.max_jerk: float = 0.0
        self.interpolation_steps: int = 0
        self.use_chunk_size: int = 0

        # 내부 상태
        self._prev_sent: dict | None = None
        self._prev_velocity: dict = {}
        self._interp_from: dict | None = None
        self._interp_to: dict | None = None
        self._interp_progress: int = 0

    def update_param(self, key: str, value) -> bool:
        """ZMQ 파라미터 업데이트. 처리했으면 True 반환."""
        if key == "lowpass_alpha":
            self.lowpass_alpha = max(0.05, min(1.0, float(value)))
            return True
        elif key == "max_velocity":
            self.max_velocity = max(0.0, min(1000.0, float(value)))
            return True
        elif key == "max_gripper_velocity":
            self.max_gripper_velocity = max(0.0, min(500.0, float(value)))
            return True
        elif key == "max_jerk":
            self.max_jerk = max(0.0, min(5000.0, float(value)))
            return True
        elif key == "interpolation_steps":
            self.interpolation_steps = max(0, min(10, int(value)))
            return True
        elif key == "use_chunk_size":
            self.use_chunk_size = max(0, min(200, int(value)))
            return True
        elif key == "fps":
            self.fps = max(1.0, min(60.0, float(value)))
            return True
        return False

    def apply(self, action_dict: dict, current_state: dict | None = None) -> dict:
        """필터 체인 적용. 원본을 수정하지 않고 새 dict 반환."""
        if action_dict is None:
            return action_dict

        result = dict(action_dict)
        target = dict(result)  # 필터 전 원본 (로그용)

        # 1. 속도 제한
        if current_state and (self.max_velocity > 0 or self.max_gripper_velocity > 0):
            joint_max_delta = self.max_velocity / self.fps if self.max_velocity > 0 else 0
            grip_max_delta = self.max_gripper_velocity / self.fps if self.max_gripper_velocity > 0 else 0
            for key in result:
                cur = current_state.get(key)
                if cur is None or not isinstance(cur, (int, float)):
                    continue
                is_gripper = "gripper" in key
                max_delta = grip_max_delta if is_gripper else joint_max_delta
                if max_delta <= 0:
                    continue
                delta = result[key] - float(cur)
                if abs(delta) > max_delta:
                    result[key] = float(cur) + max_delta * (1.0 if delta > 0 else -1.0)

        # 2. 보간
        if self.interpolation_steps > 0:
            interpolating = (self._interp_to is not None
                             and self._interp_progress < self.interpolation_steps)
            if not interpolating:
                self._interp_from = dict(self._prev_sent) if self._prev_sent else dict(result)
                self._interp_to = dict(result)
                self._interp_progress = 0

            if self._interp_from and self._interp_to and self._interp_progress < self.interpolation_steps:
                t = (self._interp_progress + 1) / self.interpolation_steps
                t_smooth = t * t * (3 - 2 * t)  # smoothstep
                result = {
                    k: self._interp_from.get(k, v) + (self._interp_to.get(k, v) - self._interp_from.get(k, v)) * t_smooth
                    for k, v in result.items()
                }
                self._interp_progress += 1

        # 3. Jerk 제한
        if self.max_jerk > 0 and self._prev_sent:
            dt = 1.0 / self.fps
            for key in result:
                if key not in self._prev_sent:
                    continue
                velocity = (result[key] - self._prev_sent[key]) / dt
                prev_vel = self._prev_velocity.get(key, velocity)
                accel = (velocity - prev_vel) / dt
                if abs(accel) > self.max_jerk:
                    clamped_accel = self.max_jerk * (1.0 if accel > 0 else -1.0)
                    velocity = prev_vel + clamped_accel * dt
                    result[key] = self._prev_sent[key] + velocity * dt
                self._prev_velocity[key] = velocity

        # 4. 로우패스 필터
        if self.lowpass_alpha < 1.0 and self._prev_sent:
            alpha = self.lowpass_alpha
            result = {
                k: alpha * v + (1 - alpha) * self._prev_sent.get(k, v)
                for k, v in result.items()
            }

        self._prev_sent = dict(result)
        return result
