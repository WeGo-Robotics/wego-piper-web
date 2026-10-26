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
   * 스트림 이름 — 사용자가 붙이고, 수집·추론이 이 카메라를 부르는 이름.
   * 영문·숫자·`_`·`-`, 다른 카메라와 겹치지 않는다. 등록할 때 정해지고(기본 `장치명_N`)
   * 카메라 페이지에서 바꾼다. 옛 별칭(`label`)을 흡수했다.
   *
   * ⚠ **LeRobot 카메라 키가 아니다.** 데이터셋 피처는 `observation.images.<키>` 로
   * 굳고 정책도 그 키로 학습되므로, 이름을 바꿔도 키는 그대로다.
   * 키는 녹화·추론 페이지에서 따로 정한다.
   */
  stream?: string
  /** 개명 이력 — 옛 이름은 같은 카메라로 해석된다. */
  aliases?: string[]
  /** 옛 필드. `stream` 과 같은 값이다(낡은 번들이 읽어도 이름이 보이게 남겼다). */
  label?: string
  /** `stream || name`. 화면에 한 줄로 쓸 표시명 — 백엔드가 계산해서 준다. */
  display_name?: string
  config?: { width: number | null; height: number | null; fps: number | null }
}

/** 드롭다운 한 줄. 이름을 앞세워 "어느 게 탑뷰인지"를 고르는 순간에 보이게 한다. */
export function camOptionText(c: ReadyCam): string {
  const n = c.stream || c.label
  return n ? `${n} — ${c.name} (${c.id})` : `${c.name} (${c.id})`
}

/**
 * 스트림 디렉토리의 한 줄 — 백엔드 `GET /api/cameras/streams` 의 짝.
 *
 * 수집·추론이 카메라를 **이 이름**으로 부른다. 등록된 카메라만 여기 있다
 * (미등록은 스트림이 아니다).
 *
 *   live     연결돼 있다 → 고를 수 있다
 *   idle     꽂혀 있는데 연결 안 됨 → 보이되 비활성 (`연결` 으로 복귀)
 *   missing  보이지 않는다(USB 빠짐) → 보이되 비활성
 */
export type StreamInfo = {
  stream: string
  /** 소비자가 여는 세그먼트 이름 — 화면은 이름(`stream`)만 쓰면 된다 */
  segment: string
  device_id: string
  device_name: string
  cam_type: string
  /** color | depth | infrared — 이름이 아니라 장치가 말하는 종류다 */
  kind: string
  state: 'live' | 'idle' | 'missing'
  /** 비활성인 이유 — 백엔드가 문장을 만든다 (`유휴로 종료됨` 은 사고가 아니라 옵션의 결과다) */
  reason: string
  /** 유휴 자동 종료(초). 0 = 끔 */
  idle_stop_s?: number
  selectable: boolean
  /** 프레임이 **지금도** 오는가. null 은 모른다. 막는 근거가 아니라 경고를 다는 근거다 */
  streaming: boolean | null
  aliases: string[]
  width: number | null
  height: number | null
  fps: number | null
}

/**
 * 저장돼 있던 매핑 값(옛 장치 id · 옛 이름 · 세그먼트 이름 · 현재 이름) → 스트림.
 * 못 찾으면 undefined — 호출부가 값을 **그대로 둔다**(지우면 "안 골랐다"로 읽혀 사유가 틀린다).
 * 백엔드 `camera_streams.resolve` 와 같은 순서다.
 */
export function findStream(value: string, streams: StreamInfo[]): StreamInfo | undefined {
  if (!value) return undefined
  const low = value.toLowerCase()
  return (
    streams.find((s) => s.device_id === value)
    ?? streams.find((s) => s.stream.toLowerCase() === low)
    ?? streams.find((s) => s.aliases.some((a) => a.toLowerCase() === low))
    ?? streams.find((s) => s.segment === value)
  )
}

/** `{키: 값}` 의 값을 현재 스트림 이름으로 맞춘다. 바뀐 게 없으면 **같은 객체**를 돌려준다(리렌더 루프 방지). */
export function normalizeMapping(
  mapping: Record<string, string>, streams: StreamInfo[],
): Record<string, string> {
  let changed = false
  const next: Record<string, string> = {}
  for (const [k, v] of Object.entries(mapping)) {
    const hit = v ? findStream(v, streams) : undefined
    next[k] = hit ? hit.stream : v
    if (next[k] !== v) changed = true
  }
  return changed ? next : mapping
}

/** `640×480@15` — 크기를 모르면 빈 문자열. fps 는 있으면 붙인다. */
export function sizeText(w?: number | null, h?: number | null, fps?: number | null): string {
  if (!w || !h) return ''
  return `${w}×${h}${fps ? `@${fps}` : ''}`
}

/** 선택기 한 줄 — `top — D435 Color 640x480@30`. 크기·fps 는 아는 것만. */
export function streamOptionText(s: StreamInfo): string {
  const dims = s.width && s.height ? ` ${s.width}x${s.height}${s.fps ? `@${s.fps}` : ''}` : ''
  return `${s.stream} — ${s.device_name}${dims}`
}
