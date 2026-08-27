import { useCallback, useEffect, useState } from 'react'
import { useSystemMessage } from './SystemMessages'
import { api } from '../services/api'

/**
 * 바닥 필터 설정 (refactor/robotd-safety.md).
 *
 * ## 이 화면이 답하는 질문
 *
 * **"지금 팔이 어디보다 아래로는 못 가나?"** 필터는 robotd 안에서 CAN 으로 나가는
 * 모든 명령에 걸린다 — 추론·녹화·수동 조작·파킹 넷 다. 그러니 그 한계가 어디인지
 * 화면에 없으면, 팔이 안 내려가는 이유를 아무도 모른다.
 *
 * ⚠ **기준면이 바닥이 아니라 팔 밑면(base_link)이다.** 그래서 기본값이 음수다 —
 *   실측에서 정상 작업이 장착면보다 3cm 가까이 아래까지 내려간다. 이 사실을 화면에
 *   안 적으면 "-4"가 오타처럼 보인다.
 */

/**
 * 켜짐/꺼짐 슬라이드 스위치.
 *
 * 저장소에 아직 이런 스위치가 없어서 여기 둔다 — 쓰는 곳이 한 곳뿐이다.
 * 두 번째가 생기면 그때 빼낸다 (`LayoutToggle` 이 그렇게 나왔다).
 *
 * ⚠ `<button role="switch">` 다. `<div onClick>` 으로 만들면 키보드로 못 켜고
 *   스크린리더가 상태를 못 읽는다. 안전 스위치라 더 그렇다.
 */
function Switch({ on, onChange, disabled }: {
  on: boolean
  onChange: (v: boolean) => void
  disabled?: boolean
}) {
  return (
    <button
      type="button" role="switch" aria-checked={on} disabled={disabled}
      onClick={() => onChange(!on)}
      className={`relative inline-flex h-6 w-11 shrink-0 items-center rounded-full
        transition-colors disabled:opacity-50
        focus:outline-none focus-visible:ring-2 focus-visible:ring-blue-500 focus-visible:ring-offset-2
        focus-visible:ring-offset-neutral-800
        ${on ? 'bg-green-500' : 'bg-neutral-600'}`}
    >
      {/* Tailwind v4 는 `translate-x-*` 를 `transform` 이 아니라 **`translate`
          속성**으로 낸다. `transition-transform` 은 v4 에서
          `transform, translate, scale, rotate` 를 모두 덮으므로 이대로 미끄러진다
          (실측 0.15s). 별도 `transform` 클래스는 v4 에서 하는 일이 없다. */}
      <span
        className={`inline-block h-4 w-4 rounded-full bg-white shadow
          transition-transform ${on ? 'translate-x-6' : 'translate-x-1'}`}
      />
    </button>
  )
}

type Floor = {
  enabled: boolean
  min_z_cm: number
  range_cm: [number, number]
  default_cm: number
}

export default function FloorGuardPanel() {
  const { notify } = useSystemMessage()
  const [floor, setFloor] = useState<Floor | null>(null)
  const [loaded, setLoaded] = useState(false)
  const [draft, setDraft] = useState('')
  const [busy, setBusy] = useState(false)

  const refresh = useCallback(() => {
    api.get<{ floor: Floor | null }>('/robots/safety')
      .then((r) => {
        setFloor(r.floor)
        if (r.floor) setDraft(String(r.floor.min_z_cm))
      })
      .catch(() => setFloor(null))
      .finally(() => setLoaded(true))
  }, [])

  useEffect(() => { refresh() }, [refresh])

  const send = async (patch: Partial<Floor>) => {
    setBusy(true)
    try {
      const r = await api.post<{ floor: Floor }>('/robots/safety', patch)
      setFloor(r.floor)
      setDraft(String(r.floor.min_z_cm))
      notify({ level: 'info', source: '안전',
        text: r.floor.enabled
          ? `바닥 필터 켜짐 — 한계 ${r.floor.min_z_cm}cm`
          : '바닥 필터 꺼짐' })
    } catch (e) {
      notify({ level: 'error', source: '안전',
        text: e instanceof Error ? e.message : '설정 변경 실패' })
      refresh()
    } finally {
      setBusy(false)
    }
  }

  if (!loaded) return <div className="text-sm text-neutral-500">불러오는 중…</div>

  // ⚠ 게이트웨이가 기본값을 지어내지 않는다 — robotd 가 없으면 팔에 아무것도
  //   안 걸려 있는데 화면만 "켜짐"이라고 말하는 상태가 제일 나쁘다.
  if (!floor) {
    return (
      <div className="space-y-2">
        <h2 className="text-lg font-semibold">바닥 필터</h2>
        <p className="text-sm text-amber-400">
          robotd 가 응답하지 않아 설정을 읽을 수 없습니다.
        </p>
        <p className="text-xs text-neutral-500">
          필터는 robotd 안에서 돕니다 — 데몬이 없으면 팔에 아무 필터도 걸려 있지
          않습니다. [서비스] 탭에서 상태를 확인하세요.
        </p>
      </div>
    )
  }

  const [lo, hi] = floor.range_cm
  const parsed = Number(draft)
  const valid = draft.trim() !== '' && Number.isFinite(parsed)
    && parsed >= lo && parsed <= hi
  const dirty = valid && parsed !== floor.min_z_cm

  return (
    <div className="space-y-4">
      <div className="flex items-start justify-between gap-4">
        <div>
          <h2 className="text-lg font-semibold">바닥 필터</h2>
          <p className="text-sm text-neutral-400">
            팔이 정해둔 높이 아래로 내려가는 명령을 <b>접촉 직전까지</b>로 줄입니다.
            추론·녹화·수동 조작·파킹 <b>전부</b>에 걸립니다.
          </p>
        </div>
        <div className="flex shrink-0 items-center gap-2">
          <span className={`text-sm ${floor.enabled ? 'text-green-400' : 'text-neutral-500'}`}>
            {floor.enabled ? '켜짐' : '꺼짐'}
          </span>
          <Switch on={floor.enabled} disabled={busy}
                  onChange={(v) => void send({ enabled: v })} />
        </div>
      </div>

      {!floor.enabled && (
        <p className="rounded border border-amber-500/40 bg-amber-500/10 px-3 py-2 text-sm text-amber-300">
          필터가 꺼져 있습니다. 관절 범위·변화율 제한과 데드맨은 그대로 돌지만,
          <b> 팔이 작업면으로 내려가는 것을 막는 것은 없습니다.</b>
        </p>
      )}

      <div className="space-y-2">
        <label className="block text-sm text-neutral-300">
          한계 높이
          <span className="ml-2 text-xs text-neutral-500">
            팔 밑면(장착면) 기준. 음수면 그 아래까지 허용합니다
          </span>
        </label>
        <div className="flex items-center gap-2">
          <input
            type="number" step="0.5" min={lo} max={hi} value={draft}
            onChange={(e) => setDraft(e.target.value)}
            onKeyDown={(e) => { if (e.key === 'Enter' && dirty) void send({ min_z_cm: parsed }) }}
            disabled={busy || !floor.enabled}
            className="w-28 rounded border border-neutral-600 bg-neutral-900 px-2 py-1
                       text-sm text-neutral-100 disabled:opacity-50"
          />
          <span className="text-sm text-neutral-400">cm</span>
          <button
            onClick={() => void send({ min_z_cm: parsed })}
            disabled={busy || !dirty || !floor.enabled}
            className="rounded bg-blue-600 px-3 py-1 text-sm text-white
                       hover:bg-blue-500 disabled:opacity-40"
          >
            적용
          </button>
          {floor.min_z_cm !== floor.default_cm && (
            <button
              onClick={() => void send({ min_z_cm: floor.default_cm })}
              disabled={busy || !floor.enabled}
              className="rounded bg-neutral-700 px-3 py-1 text-sm text-neutral-300
                         hover:bg-neutral-600 disabled:opacity-40"
            >
              기본값 ({floor.default_cm})
            </button>
          )}
        </div>
        {!valid && draft.trim() !== '' && (
          <p className="text-xs text-red-400">{lo} ~ {hi} 사이여야 합니다</p>
        )}
      </div>

      {/* ⚠ 이 문단이 없으면 음수 기본값이 오타로 보인다. 숫자가 실측에서 나왔다는
          것과, 그 3cm 이 아직 안 풀렸다는 것을 같이 적는다. */}
      <div className="space-y-1 rounded border border-neutral-700 bg-neutral-900/60 px-3 py-2
                      text-xs leading-relaxed text-neutral-400">
        <p>
          기준은 <b>바닥이 아니라 팔 밑면</b>입니다. 실측(데이터셋 4개·84,065프레임)에서
          정상 작업이 장착면보다 <b>3cm 가까이 아래</b>까지 내려갑니다 — 그리퍼 끝이
          작업면의 물체를 집을 때입니다. 그래서 기본값이 −4cm 입니다.
        </p>
        <p>
          0cm 로 두면 정상 작업의 4.4% 가 막힙니다. 값을 올릴수록 안전 여유는 늘지만
          팔이 작업면에 못 닿습니다.
        </p>
        <p className="text-amber-400/80">
          이 3cm 이 받침대 높이인지 캘리브레이션 오차인지는 아직 확인되지 않았습니다.
          장착면에서 작업면까지 줄자로 재면 갈립니다 — 후자라면 이 숫자의 절대 높이가
          통째로 틀린 것입니다.
        </p>
      </div>
    </div>
  )
}
