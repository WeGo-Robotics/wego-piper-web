/**
 * 지금 이 카메라가 **어떻게 노출되고 있는가** — 노출 눈금(스톱) + 셔터 + 게인.
 *
 * 0.0 EV 의 기준은 회색카드 보정이 맞추는 목표(`graycard.TARGET_LUMA`)와 같다.
 * 화면 두 곳이 같은 카메라를 두고 다른 말을 하면 안 되기 때문이다.
 *
 * ⚠ 값은 게이트웨이 샘플러가 재 둔 것을 받아 쓴다 — 이 컴포넌트가 장치를
 *   묻지 않는다. 카메라 카드마다 컨트롤을 질의하면 D405 를 D-state 로 물린다.
 */

export const METERING_MODES = ['average', 'center', 'spot'] as const
export type Metering = typeof METERING_MODES[number]

export const METERING_LABEL: Record<Metering, string> = {
  average: '평균',
  center: '중앙중점',
  spot: '스팟',
}

export type LightSample = {
  id: string
  luma: number
  sat_pct: number
  metering?: Record<string, number>
  ev?: Record<string, number>
  exposure_us?: number
  gain?: number
}

/** 눈금의 양끝. 백엔드 `lighting.EV_LIMIT` 과 같아야 한다. */
const EV_LIMIT = 5

/** ⚠ 잘린 화소는 자기가 원래 얼마나 밝았는지 **말할 수 없다.** 이만큼 넘게
 *  포화되면 측광값이 실제보다 낮게 나오고, 그건 어느 모드로도 안 고쳐진다. */
const SAT_UNRELIABLE_PCT = 10

const shutter = (us?: number) =>
  us == null ? null : us >= 1000 ? `${(us / 1000).toFixed(1)}ms` : `${Math.round(us)}µs`

const signed = (v: number) => `${v >= 0 ? '+' : '−'}${Math.abs(v).toFixed(1)}`

export default function ExposureReadout({ light, mode }: {
  light?: LightSample
  mode: Metering
}) {
  // ⚠ 아직 안 잰 카메라와 "노출 0" 은 다르다 — 모르면 아무것도 안 그린다.
  const ev = light?.ev?.[mode]
  if (!light || ev == null) return null

  // 한두 스톱은 흔하고 문제도 아니다. 두 스톱부터만 눈에 띄게 한다 —
  // 늘 켜져 있는 표시가 늘 경고색이면 아무도 안 본다.
  const off = Math.abs(ev) >= 2
  const clipped = light.sat_pct >= SAT_UNRELIABLE_PCT
  const pct = ((ev + EV_LIMIT) / (2 * EV_LIMIT)) * 100

  return (
    <div className="flex flex-wrap items-center gap-x-2 gap-y-0.5 text-[11px]
                    tabular-nums text-neutral-400"
         title={`${METERING_LABEL[mode]} 측광 ${signed(ev)} EV (−${EV_LIMIT}.0 ~ +${EV_LIMIT}.0, 0.0 = 목표 노출)
잰 밝기 ${light.metering?.[mode] ?? light.luma}/255 · 포화 ${light.sat_pct}%
0 점은 회색카드 보정의 목표와 같습니다`}>
      <span className="text-neutral-500">{METERING_LABEL[mode]}</span>
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

      {/* ⚠ 측광이 수상해 보이는 이유가 대개 이것이다 — 모드를 바꿔도 안 고쳐진다. */}
      {clipped && (
        <span className="text-amber-500"
              title="잘린 화소는 원래 밝기를 말할 수 없어 측광값이 실제보다 낮게 나옵니다. 노출을 줄이세요.">
          포화 {light.sat_pct}%
        </span>
      )}
    </div>
  )
}
