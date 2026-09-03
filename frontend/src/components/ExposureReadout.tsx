/**
 * 지금 이 카메라가 **어떻게 노출되고 있는가** — 노출 눈금(스톱) + 셔터 + 게인.
 *
 * 0.0 EV 의 기준은 회색카드 보정이 맞추는 목표(`graycard.TARGET_LUMA`)와 같다.
 * 화면 두 곳이 같은 카메라를 두고 다른 말을 하면 안 되기 때문이다.
 *
 * ⚠ 값은 게이트웨이 샘플러가 재 둔 것을 받아 쓴다 — 이 컴포넌트가 장치를
 *   묻지 않는다. 카메라 카드마다 컨트롤을 질의하면 D405 를 D-state 로 물린다.
 */

export type LightSample = {
  id: string
  luma: number
  ev?: number
  exposure_us?: number
  gain?: number
}

/** 눈금의 양끝. 백엔드 `lighting.EV_LIMIT` 과 같아야 한다. */
const EV_LIMIT = 5

const shutter = (us?: number) =>
  us == null ? null : us >= 1000 ? `${(us / 1000).toFixed(1)}ms` : `${Math.round(us)}µs`

const signed = (v: number) => `${v >= 0 ? '+' : '−'}${Math.abs(v).toFixed(1)}`

export default function ExposureReadout({ light }: { light?: LightSample }) {
  // ⚠ 아직 안 잰 카메라와 "노출 0" 은 다르다 — 모르면 아무것도 안 그린다.
  if (!light || light.ev == null) return null

  const ev = light.ev
  // 한두 스톱은 흔하고 문제도 아니다. 두 스톱부터만 눈에 띄게 한다 —
  // 늘 켜져 있는 표시가 늘 경고색이면 아무도 안 본다.
  const off = Math.abs(ev) >= 2
  const pct = ((ev + EV_LIMIT) / (2 * EV_LIMIT)) * 100

  return (
    <div className="flex items-center gap-2 text-[11px] tabular-nums text-neutral-400"
         title={`노출 눈금 ${signed(ev)} EV (−${EV_LIMIT}.0 ~ +${EV_LIMIT}.0, 0.0 = 목표 노출)
평균 밝기 ${light.luma}/255
0 점은 회색카드 보정의 목표와 같습니다`}>
      <span className={off ? 'font-semibold text-amber-400' : 'text-neutral-200'}>
        {signed(ev)} EV
      </span>

      {/* −5 … 0 … +5 눈금. 숫자만으로는 "얼마나 치우쳤나"가 안 읽힌다. */}
      <span className="relative h-1 w-16 shrink-0 rounded-full bg-neutral-700">
        <span className="absolute inset-y-0 left-1/2 w-px -translate-x-1/2 bg-neutral-500" />
        <span className={`absolute top-1/2 h-2 w-2 -translate-x-1/2 -translate-y-1/2
                          rounded-full ${off ? 'bg-amber-400' : 'bg-neutral-200'}`}
              style={{ left: `${pct}%` }} />
      </span>

      {light.exposure_us != null && (
        <span title="셔터 (노출 시간)">⏱ {shutter(light.exposure_us)}</span>
      )}
      {light.gain != null && <span title="게인">게인 {light.gain}</span>}
    </div>
  )
}
