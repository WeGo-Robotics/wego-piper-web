/**
 * 등록된 카메라 — 백엔드 `CameraInfo.to_dict()` 의 짝.
 *
 * 이전에는 같은 타입이 RecordingPage / InferencePage / EncoderProbePage 에
 * 각각 복사돼 있어서, 필드를 하나 추가하려면 세 군데를 고쳐야 했다.
 */

export type ReadyCam = {
  id: string
  /** 하드웨어가 말하는 이름 — "D435 Color" */
  name: string
  /**
   * 사람이 붙인 별칭 — "탑뷰", "손목".
   *
   * ⚠ **LeRobot 카메라 키가 아니다.** 데이터셋 피처는 `observation.images.<키>` 로
   * 굳고 정책도 그 키로 학습되므로, 별칭을 바꿔도 키는 그대로다.
   * 키는 녹화·추론 페이지에서 따로 정한다.
   */
  label?: string
  /** `label || name`. 화면에 한 줄로 쓸 표시명 — 백엔드가 계산해서 준다. */
  display_name?: string
  config?: { width: number | null; height: number | null; fps: number | null }
}

/** 드롭다운 한 줄. 별칭이 있으면 앞세워 "어느 게 탑뷰인지"를 고르는 순간에 보이게 한다. */
export function camOptionText(c: ReadyCam): string {
  return c.label ? `${c.label} — ${c.name} (${c.id})` : `${c.name} (${c.id})`
}
