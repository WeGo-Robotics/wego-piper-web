/** 페이즈 이름·색 — 라벨 화면(인덱스)과 추론 텔레메트리(이름)가 함께 쓴다.
 *
 * ⚠ **인덱스가 곧 페이즈 코드다** (piper_phase.PHASE_NAMES 순서).
 *   페이즈를 늘리면 여기도 늘려야 한다 — 짧으면 `undefined` 가 되어 구간이
 *   **투명하게** 그려지고, 라벨이 없는 것처럼 보인다.
 */
export const PHASE_COLORS = [
  '#525252',  // IDLE
  '#3b82f6',  // APPROACH
  '#22d3ee',  // ALIGN
  '#f59e0b',  // GRASP
  '#22c55e',  // HOLD
  '#a855f7',  // RELEASE
  '#404040',  // DONE
  '#78716c',  // PARKING — 복귀. DONE 과 이웃하되 구분되게 (둘 다 무채색 계열)
]

const DEFAULT_NAMES = [
  'IDLE', 'APPROACH', 'ALIGN', 'GRASP', 'HOLD', 'RELEASE', 'DONE', 'PARKING',
]

/** 이름으로 색을 찾는다.
 *
 * 텔레메트리는 인덱스가 아니라 **이름**을 보낸다 — 체크포인트가 자기 `stage_names`
 * 를 갖고 있어서, 다른 태스크로 구운 모델이면 여기 없는 이름이 올 수 있다.
 * 그 경우 회색으로 떨어뜨린다: 색이 틀린 것보다 색이 없는 편이 낫다.
 */
export function phaseColor(name: string | undefined): string {
  const i = name ? DEFAULT_NAMES.indexOf(name) : -1
  return i >= 0 ? PHASE_COLORS[i] : '#525252'
}
