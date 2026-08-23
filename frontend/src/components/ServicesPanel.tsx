import { useCallback, useEffect, useState } from 'react'
import { useSystemMessage } from './SystemMessages'
import { api } from '../services/api'

/**
 * 서비스 상태와 재시작 (feature/service-restart.md).
 *
 * ## 이 화면이 답하는 질문 하나
 *
 * **"지금 도는 코드가 최신인가?"** 유닛은 기동 시점의 코드로 돈다. 파일을 고쳐도
 * 재시작 전에는 아무 일도 안 일어나는데, 그 사실이 지금까지 화면 어디에도 없었다.
 * rsd 가 이틀 전 코드로 돌아 고친 버그가 그대로 재현된 적이 있고, 게이트웨이가
 * 새 라우트를 모른 채 404 를 돌려준 적이 있다. 둘 다 한참을 헤맸다.
 */

type Unit = {
  name: string; active: boolean; since: number | null; stale: boolean
  age_s: number | null; pid: number | null; description: string
}
type Gateway = {
  pid: number; since: number | null; stale: boolean
  age_s: number | null; restartable: boolean
}

function age(sec: number | null): string {
  if (sec === null) return '?'
  if (sec < 60) return `${sec}초`
  if (sec < 3600) return `${Math.round(sec / 60)}분`
  if (sec < 86400) return `${(sec / 3600).toFixed(1)}시간`
  return `${(sec / 86400).toFixed(1)}일`
}

export default function ServicesPanel() {
  const { notify, confirm } = useSystemMessage()
  const [units, setUnits] = useState<Unit[]>([])
  const [gateway, setGateway] = useState<Gateway | null>(null)
  const [busy, setBusy] = useState<string | null>(null)

  const refresh = useCallback(() => {
    api.get<{ units: Unit[]; gateway: Gateway }>('/system/services')
      .then((r) => { setUnits(r.units ?? []); setGateway(r.gateway ?? null) })
      .catch(() => {})
  }, [])

  useEffect(() => { refresh() }, [refresh])

  const restartUnit = async (name: string) => {
    setBusy(name)
    try {
      await api.post('/system/services/restart', { name })
      notify({ level: 'info', text: `${name} 재시작됨`, source: '서비스' })
      // 유닛이 올라오고 상태가 잡힐 틈을 준다
      setTimeout(refresh, 1500)
    } catch (e) {
      notify({ level: 'error', source: '서비스',
        text: e instanceof Error ? e.message : `${name} 재시작 실패` })
    } finally { setBusy(null) }
  }

  const restartGateway = async () => {
    // ⚠ `window.confirm` 을 쓰지 않는다 — 이벤트 루프를 막아 E-stop heartbeat 가
    //   끊긴다. 논블로킹 모달이다.
    const yes = await confirm(
      '게이트웨이를 재시작합니다.\n\n' +
      '몇 초 동안 화면이 끊기고, 그동안 들어온 요청은 실패합니다.\n' +
      '돌고 있는 학습·정책서버는 별도 유닛이라 영향받지 않습니다.')
    if (!yes) return
    setBusy('gateway')
    try {
      await api.post('/system/restart', {})
      notify({ level: 'warn', source: '서비스',
        text: '게이트웨이를 재시작하는 중입니다 — 잠시 뒤 새로고침하세요.' })
    } catch (e) {
      notify({ level: 'error', source: '서비스',
        text: e instanceof Error ? e.message : '재시작 실패' })
    } finally { setBusy(null) }
  }

  const staleCount = units.filter((u) => u.stale).length + (gateway?.stale ? 1 : 0)

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between gap-3">
        <div>
          <h2 className="text-lg font-semibold">서비스</h2>
          <p className="mt-1 text-xs text-neutral-400">
            유닛은 <b>기동 시점의 코드</b>로 돕니다. 코드를 고쳤으면 재시작해야 반영됩니다.
          </p>
        </div>
        <button onClick={refresh}
          className="px-2 py-1 text-xs rounded bg-neutral-700 hover:bg-neutral-600">
          새로고침
        </button>
      </div>

      {staleCount > 0 && (
        <p className="rounded border border-amber-500/40 bg-amber-500/10 px-3 py-2
                      text-xs text-amber-300">
          ⚠ {staleCount}개가 <b>고친 코드보다 먼저</b> 떠 있습니다 — 재시작 전에는
          그 변경이 반영되지 않습니다.
        </p>
      )}

      <div className="space-y-1.5">
        {gateway && (
          <Row
            name="게이트웨이 (웹 서버)" active stale={gateway.stale}
            detail={`pid ${gateway.pid} · ${age(gateway.age_s)} 전 기동`}
            busy={busy === 'gateway'}
            disabled={!gateway.restartable}
            onRestart={restartGateway}
          />
        )}
        {units.map((u) => (
          <Row key={u.name} name={u.name} active={u.active} stale={u.stale}
            detail={`${u.active ? `${age(u.age_s)} 전 기동` : '멈춤'}${
              u.pid ? ` · pid ${u.pid}` : ''}`}
            busy={busy === u.name}
            onRestart={() => restartUnit(u.name)} />
        ))}
        {units.length === 0 && !gateway && (
          <p className="text-xs text-neutral-500">systemd 유닛을 찾지 못했습니다.</p>
        )}
      </div>
    </div>
  )
}

function Row({ name, active, stale, detail, busy, disabled, onRestart }: {
  name: string; active: boolean; stale: boolean; detail: string
  busy: boolean; disabled?: boolean; onRestart: () => void
}) {
  return (
    <div className={`flex items-center gap-3 rounded border px-3 py-2 ${stale
      ? 'border-amber-500/40 bg-amber-500/5' : 'border-neutral-700 bg-neutral-800'}`}>
      <span className={`h-2 w-2 shrink-0 rounded-full ${active
        ? 'bg-emerald-500' : 'bg-neutral-600'}`} aria-hidden />
      <div className="min-w-0 flex-1">
        <p className="truncate text-sm text-neutral-200">
          {name}
          {stale && <span className="ml-2 text-xs text-amber-400">코드가 더 새것</span>}
        </p>
        <p className="text-[11px] text-neutral-500 tabular-nums">{detail}</p>
      </div>
      <button onClick={onRestart} disabled={busy || disabled}
        title={disabled ? '이 방식으로는 재시작할 수 없습니다' : undefined}
        className={`shrink-0 px-2 py-1 text-xs rounded disabled:bg-neutral-700
          disabled:text-neutral-500 text-white ${stale
            ? 'bg-amber-600 hover:bg-amber-500' : 'bg-blue-600 hover:bg-blue-500'}`}>
        {busy ? '재시작 중…' : '재시작'}
      </button>
    </div>
  )
}
