import { useCallback, useEffect, useRef, useState } from 'react'
import { ErrorChart, RateChart, RX_COLOR, TX_COLOR, type Point } from './BusCharts'
import { useSystemMessage } from './SystemMessages'
import { api } from '../services/api'

/**
 * CAN 버스 상태 — 지금 상태 + 누적 오류 + 트래픽.
 *
 * ⚠ **카운터만 보면 오독한다.** 인터페이스를 다시 열면 0 으로 돌아가므로
 *   절대값끼리 비교하면 안 된다 — 실측: can2·can3 이 1초 차이로 올라왔는데
 *   각각 0 과 34,794 였다. 그래서 **백만 프레임당 오류**를 같이 보여준다.
 *   가동 시간이 달라도 그건 비교가 된다.
 */

type Counters = Record<string, number>
type Bus = {
  iface: string; state: string | null; healthy: boolean
  bitrate: number | null; counters: Counters; errors_total: number | null
  rx_packets: number | null; tx_packets: number | null
  rx_errors: number | null; tx_errors: number | null
  rx_dropped: number | null; tx_dropped: number | null
  /** 초기화 이후의 오류. 카운터 자체는 안 지워지므로 이게 진짜 비교값이다. */
  errors_since_reset?: number
  counters_since_reset?: Counters
  rx_since_reset?: number
  tx_since_reset?: number
  /** 데몬이 2초마다 모아 둔 초당 증가량 — 탭을 열면 **바로** 그려진다.
   *  브라우저가 모으면 열 때마다 빈 그래프로 시작하고, 정작 보고 싶은
   *  "내가 안 보던 동안" 이 비어 있다. */
  history?: Point[]
}

const COUNTER_LABEL: Record<string, string> = {
  restarts: '재시작', bus_errors: '버스 오류', arbitration_lost: '조정 실패',
  error_warning: '경고', error_passive: '수동', bus_off: '버스 오프',
}

/** 로깅 창 (분). 데몬은 늘 가장 긴 것(30분)을 모으고, 여기서 고른 만큼만 받는다 —
 *  그래야 5분으로 보다가 30분으로 바꾸는 순간 그 25분이 비어 있지 않다. */
const WINDOWS = [5, 10, 30] as const

/** 자동 새로고침 주기 후보. 버스 상태는 초 단위로 급변하지 않는다. */
const INTERVALS = [2, 5, 15] as const

const num = (v: number | null | undefined) =>
  v == null ? '—' : v.toLocaleString('ko-KR')

/**
 * 백만 프레임당 오류. 트래픽이 없으면 뜻이 없으므로 null.
 *
 * ⚠ **기준선이 있으면 분자와 분모가 같은 기준이어야 한다.** 누적 오류를 누적
 *   RX 로 나누던 때 `50,796,791/M` 이 떴다 — 다른 항목은 초기화 이후를 쓰는데
 *   이것만 옛 숫자였다. 기준을 잡을 거면 전부 잡는다.
 */
function perMillion(bus: Bus): number | null {
  const errs = bus.errors_since_reset ?? bus.errors_total
  const rx = bus.rx_since_reset ?? bus.rx_packets
  if (!rx || errs == null) return null
  return (errs / rx) * 1e6
}

export default function BusStatusPanel() {
  const { notify, confirm } = useSystemMessage()
  const [buses, setBuses] = useState<Bus[]>([])
  const [err, setErr] = useState<string | null>(null)
  const [at, setAt] = useState<Date | null>(null)
  const [busy, setBusy] = useState(false)
  // ⚠ **기본이 켜짐이다.** 데몬이 이미 2초마다 재고 있으므로 이 폴링은 모아 둔
  //   버퍼를 읽는 것뿐이라 싸다 — 예전에 기본을 꺼 뒀던 이유(인터페이스마다
  //   `ip` 를 부른다)는 그 비용이 데몬으로 옮겨가면서 사라졌다.
  const [auto, setAuto] = useState(true)
  const [every, setEvery] = useState<number>(2)
  const [minutes, setMinutes] = useState<number>(5)
  const [resetting, setResetting] = useState<string | null>(null)
  const load = useCallback(async () => {
    setBusy(true)
    try {
      const d = await api.get<{ buses: Bus[] }>(`/robots/bus?minutes=${minutes}`)
      setBuses(d.buses); setErr(null); setAt(new Date())

    } catch (e) {
      setErr(e instanceof Error ? e.message : '읽지 못했습니다')
    } finally { setBusy(false) }
  }, [minutes])

  useEffect(() => { void load() }, [load])

  const timer = useRef<ReturnType<typeof setInterval>>(undefined)
  useEffect(() => {
    clearInterval(timer.current)
    if (auto) timer.current = setInterval(() => void load(), every * 1000)
    return () => clearInterval(timer.current)
  }, [auto, every, load])

  // ⚠ 초기화는 **연결을 끊는다.** 카운터가 0 이 되는 것은 부작용이 아니라 목적의
  //   절반이다 — 누적값은 인터페이스를 올린 뒤부터의 합이라, 고쳤는지 보려면
  //   기준을 다시 잡아야 한다. 다만 그 사실을 누르기 전에 말해야 한다.
  const reset = async (iface: string) => {
    if (!await confirm(
      `${iface} 버스를 내렸다 올립니다.\n\n` +
      '· 이 버스의 팔 **연결이 끊깁니다** — 디바이스 탭에서 다시 연결하세요\n' +
      '· CAN 컨트롤러가 다시 세워집니다 (BUS-OFF 에서 빠져나오는 유일한 길입니다 —\n' +
      '  이 어댑터는 자동 복구를 지원하지 않습니다)\n' +
      '· 누적 오류 카운터는 **안 지워집니다** — 대신 "초기화 이후" 를 세기 시작합니다\n\n' +
      '계속할까요?')) return
    setResetting(iface)
    try {
      await api.post('/robots/bus/reset', { iface })
      await load()
      notify({ level: 'info', source: '버스',
               text: `${iface} 를 초기화했습니다 — 팔을 다시 연결하세요.` })
    } catch (e) {
      notify({ level: 'error', source: '버스',
               text: e instanceof Error ? e.message : '초기화 실패' })
    } finally { setResetting(null) }
  }

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center gap-3">
        <label className="flex items-center gap-1.5 text-xs text-neutral-300">
          <input type="checkbox" checked={auto} onChange={(e) => setAuto(e.target.checked)}
            className="accent-blue-500" />
          자동 새로고침
        </label>
        <div className="flex items-center gap-1 rounded bg-neutral-800 p-0.5">
          {INTERVALS.map((s) => (
            <button key={s} onClick={() => { setEvery(s); setAuto(true) }}
              className={`rounded px-2 py-1 text-xs transition-colors ${
                auto && every === s ? 'bg-neutral-600 text-white'
                                    : 'text-neutral-400 hover:text-neutral-200'}`}>
              {s}초
            </button>
          ))}
        </div>
        <div className="flex items-center gap-1 rounded bg-neutral-800 p-0.5"
             title="그래프가 보여줄 기간. 데몬은 늘 30분치를 모으므로 바꾸면 과거가 바로 보입니다.">
          <span className="pl-1.5 text-[10px] text-neutral-500">기간</span>
          {WINDOWS.map((m) => (
            <button key={m} onClick={() => setMinutes(m)}
              className={`rounded px-2 py-1 text-xs transition-colors ${minutes === m
                ? 'bg-neutral-600 text-white' : 'text-neutral-400 hover:text-neutral-200'}`}>
              {m}분
            </button>
          ))}
        </div>
        <button onClick={() => void load()} disabled={busy}
          className="rounded bg-neutral-700 px-3 py-1 text-xs text-neutral-300
                     hover:bg-neutral-600 disabled:opacity-50">
          {busy ? '읽는 중…' : '↻ 새로고침'}
        </button>
        <span className="text-xs text-neutral-500">
          {at ? `${at.toLocaleTimeString('ko-KR')} 기준` : '아직 안 읽음'}
        </span>
      </div>

      {err && <p className="text-xs text-red-400">{err}</p>}

      {buses.length === 0 && !err ? (
        <p className="text-xs text-neutral-500">CAN 인터페이스가 없습니다.</p>
      ) : buses.map((b) => {
        const rate = perMillion(b)
        return (
          <div key={b.iface} className="rounded-lg border border-neutral-700 bg-neutral-800 p-3 space-y-2">
            <div className="flex flex-wrap items-center gap-2">
              <span className="font-mono text-sm text-neutral-100">{b.iface}</span>
              <span className={`rounded px-2 py-0.5 text-xs ${b.healthy
                ? 'bg-green-600/20 text-green-400' : 'bg-red-600/20 text-red-400'}`}>
                {b.state ?? '알 수 없음'}
              </span>
              {b.bitrate && (
                <span className="text-xs text-neutral-500">{b.bitrate / 1000}kbps</span>
              )}
              <span className="ml-auto text-xs tabular-nums text-neutral-400"
                    title={b.rx_since_reset != null
                      ? `초기화 이후 · 누적 RX ${num(b.rx_packets)} / TX ${num(b.tx_packets)}`
                      : '누적'}>
                RX {num(b.rx_since_reset ?? b.rx_packets)} · TX {num(b.tx_since_reset ?? b.tx_packets)}
              </span>
              <button onClick={() => void reset(b.iface)} disabled={resetting !== null}
                title="버스를 내렸다 올립니다 — 팔 연결이 끊기고 카운터가 0 이 됩니다"
                className="rounded bg-neutral-700 px-2 py-0.5 text-xs text-neutral-400
                           hover:bg-amber-600 hover:text-white disabled:opacity-50">
                {resetting === b.iface ? '초기화 중…' : '초기화'}
              </button>
            </div>

            {/* ⚠ **기준선이 있으면 그쪽이 주인공이다.** 누적값은 down/up 으로 안
                지워지므로, 초기화한 뒤에도 1억이 넘는 숫자가 주황색으로 남아
                "초기화가 소용없다" 로 읽힌다. 실제로 그렇게 보고됐다.
                지금 고쳐졌는지를 보려면 초기화 이후만 봐야 한다. */}
            <div className="flex flex-wrap gap-x-4 gap-y-1 text-xs tabular-nums">
              {Object.entries(b.counters_since_reset ?? b.counters).map(([k, v]) => (
                <span key={k} className={v ? 'text-amber-400' : 'text-neutral-500'}
                      title={b.counters_since_reset
                        ? `초기화 이후 ${num(v)} · 누적 ${num(b.counters[k])}`
                        : `누적 ${num(v)}`}>
                  {COUNTER_LABEL[k] ?? k} <b>{num(v)}</b>
                </span>
              ))}
              {b.counters_since_reset && (
                <span className="text-neutral-600">
                  (초기화 이후 · 누적 {num(b.errors_total)})
                </span>
              )}
            </div>

            <div className="flex flex-wrap gap-x-4 gap-y-1 text-xs tabular-nums text-neutral-500">
              <span>드롭 RX {num(b.rx_dropped)} / TX {num(b.tx_dropped)}</span>
              <span>오류 RX {num(b.rx_errors)} / TX {num(b.tx_errors)}</span>
              {rate != null && (
                <span title="누적 카운터는 인터페이스를 다시 열면 0 이 됩니다 — 절대값끼리 비교하면 안 되고, 이 값이 가동 시간과 무관하게 비교됩니다."
                      className={rate > 1 ? 'text-amber-400' : ''}>
                  백만 프레임당 오류 <b>{rate.toFixed(2)}</b>
                </span>
              )}
            </div>
            <div className="flex flex-wrap gap-4 pt-1">
              <div>
                <div className="mb-1 flex items-center gap-3 text-[10px] text-neutral-400">
                  <span>초당 패킷 · 최근 {minutes}분</span>
                  {/* 2계열이므로 범례는 늘 있고, 최신값을 직접 붙인다 —
                      TX 가 바닥에 붙어도 숫자는 읽힌다 */}
                  <span className="flex items-center gap-1">
                    <i className="inline-block h-2 w-2 rounded-full" style={{ background: '#3987e5' }} />
                    RX {(b.history?.at(-1)?.rx ?? 0).toFixed(0)}
                  </span>
                  <span className="flex items-center gap-1">
                    <i className="inline-block h-2 w-2 rounded-full" style={{ background: '#d95926' }} />
                    TX {(b.history?.at(-1)?.tx ?? 0).toFixed(1)}
                  </span>
                </div>
                {/* ⚠ **소형 다중이다.** RX 는 초당 수천, TX 는 0 에 가깝다 —
                    한 축에 얹으면 TX 가 바닥에 깔려 안 보이고, 축을 둘로 쪼개면
                    이중 축이 된다. 각자 자기 배율의 별개 플롯이 답이다. */}
                <RateChart points={b.history ?? []} field="rx" color={RX_COLOR} />
                <RateChart points={b.history ?? []} field="tx" color={TX_COLOR} />
              </div>
              <div>
                <div className="mb-1 text-[10px] text-neutral-400">
                  오류 {b.counters_since_reset ? '(초기화 이후)' : '(누적)'}
                </div>
                <ErrorChart counters={b.counters_since_reset ?? b.counters}
                            labels={COUNTER_LABEL} />
              </div>
            </div>
          </div>
        )
      })}

      <p className="text-xs text-neutral-600">
        ⚠ 누적 카운터는 <b>버스를 초기화해도 안 지워집니다</b> — 이 어댑터(gs_usb)의
        드라이버가 그렇습니다. 초기화하면 그 시점을 기준으로 다시 세기 시작하고,
        위 숫자는 <b>그 이후</b>의 값입니다(괄호 안이 누적). 팔끼리 비교할 때는
        절대값이 아니라 <b>백만 프레임당 오류</b>를 보세요 — 실측으로 같은 시각에
        올라온 두 버스가 0 과 34,794 였습니다.
      </p>
    </div>
  )
}
