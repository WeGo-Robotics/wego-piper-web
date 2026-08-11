/**
 * Piper 관절 정의 — 이름·라벨·정규화 범위의 단일 소스.
 *
 * 이전에는 같은 7관절이 세 파일에 각각 있었다 (RobotsPage, TelemetryPanel,
 * ManualControlPanel). 표현만 달랐다 — `'joint1'` vs `'joint1.pos'`.
 * `config/pages.ts` (커밋 181ace1) 와 같은 방식의 선언 리스트로 통합한다.
 *
 * ⚠ **순서를 바꾸면 안 된다.** `ManualControlPanel` 이 `currentJoints[i]` 로
 * 인덱스 매칭하고, 백엔드 `app/core/joints.py` 의 `JOINT_ORDER` 도 같은 순서다.
 * 순서가 어긋나면 수동 제어 슬라이더가 엉뚱한 관절을 움직인다.
 *
 * 범위는 **정규화 값**이다 (관절 -100..100, 그리퍼 0..100).
 * raw 엔코더 범위는 백엔드에만 있다 — 프론트는 raw 를 보지 않는다.
 */

export type Joint = {
  /** 텔레메트리·로봇 페이지에서 쓰는 이름 */
  name: string
  /** 백엔드 action dict 키 (`/api/params` 의 manual_action) */
  actionKey: string
  label: string
  min: number
  max: number
}

export const JOINTS: Joint[] = [
  { name: 'joint1', actionKey: 'joint1.pos', label: 'Joint 1', min: -100, max: 100 },
  { name: 'joint2', actionKey: 'joint2.pos', label: 'Joint 2', min: -100, max: 100 },
  { name: 'joint3', actionKey: 'joint3.pos', label: 'Joint 3', min: -100, max: 100 },
  { name: 'joint4', actionKey: 'joint4.pos', label: 'Joint 4', min: -100, max: 100 },
  { name: 'joint5', actionKey: 'joint5.pos', label: 'Joint 5', min: -100, max: 100 },
  { name: 'joint6', actionKey: 'joint6.pos', label: 'Joint 6', min: -100, max: 100 },
  { name: 'gripper', actionKey: 'gripper.pos', label: 'Gripper', min: 0, max: 100 },
]

export const JOINT_NAMES: string[] = JOINTS.map((j) => j.name)

export const JOINT_RANGE: Record<string, [number, number]> = Object.fromEntries(
  JOINTS.map((j) => [j.name, [j.min, j.max] as [number, number]]),
)
