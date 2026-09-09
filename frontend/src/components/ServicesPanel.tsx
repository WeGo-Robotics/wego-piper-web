import { useCallback, useEffect, useState } from 'react'
import { useSystemMessage } from './SystemMessages'
import { api } from '../services/api'

/**
 * 서비스 — 상태·재시작·켜기/끄기·부팅 시 시작 (feature/services.md, service-restart.md).
 *
 * ## 이 화면이 답하는 질문 둘
 *
 * **"지금 도는 코드가 최신인가?"** 유닛은 기동 시점의 코드로 돈다. rsd 가 이틀 전
 * 코드로 돌아 고친 버그가 재현된 적이 있고, 게이트웨이가 새 라우트를 모른 채 404 를
 * 돌려준 적이 있다.
 *
 * **"이 기계에서 무엇을 돌릴 것인가?"** 시뮬레이션(simd)·SO-101(so101d) 같은
 * 선택 데몬은 설치는 되지만 처음엔 꺼져 있다. 여기서 켜고, "부팅 시 시작"을 고른다.
 * 켜기/끄기는 호스트의 `piper-unitd` 가 실행한다 — 컨테이너 게이트웨이는 systemctl
 * 이 없다. unitd 가 없으면 버튼이 잠기고 그 이유가 보인다.
 *
 * ⚠ estopd 는 상태만 본다. 안전장치를 웹에서 끄는 경로는 만들지 않는다.
 */

type Unit = {
  name: string; active: boolean; since: number | null; stale: boolean
  age_s: number | null; pid: number | null; description: string
  // 컨테이너 배포에서는 버스 생존 키로만 보인다 — 기동 시각을 모르고 재시작도 불가
  restartable?: boolean
  kind?: 'core' | 'optional'
  enabled?: boolean | null       // 부팅 시 시작. null = 모른다
  controllable?: boolean         // unitd 가 있고, 읽기 전용이 아니다
  installed?: boolean
  readonly?: boolean
}
type Gateway = {
  pid: number; since: number | null; stale: boolean
  age_s: number | null; restartable: boolean
}
type Action = 'start' | 'stop' | 'restart' | 'enable' | 'disable'

function age(sec: number | null): string {
  if (sec === null) return '?'
  if (sec < 60) return `${sec}초`
  if (sec < 3600) return `${Math.round(sec / 60)}분`
  if (sec < 86400) return `${(sec / 3600).toFixed(1)}시간`
  return `${(sec / 86400).toFixed(1)}일`
}

const ACTION_LABEL: Record<Action, string> = {
  start: '켜짐', stop: '꺼짐', restart: '재시작됨', enable: '부팅 시 시작', disable: '부팅 시 시작 해제',
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

  const act = async (u: Unit, action: Action) => {
    // 핵심 데몬을 끄는 건 장치가 사라지는 일이다 — 한 번 묻는다. 논블로킹 모달
    // (window.confirm 은 이벤트 루프를 막아 E-stop heartbeat 가 끊긴다).
    if (action === 'stop' && u.kind !== 'optional') {
      const yes = await confirm(
        `${u.name} 을(를) 끕니다.\n\n` +
        '이 장치(팔·카메라)는 다시 켤 때까지 화면에서 사라집니다.\n' +
        '부팅 시 시작 설정은 그대로입니다 — 재부팅하면 다시 뜹니다.')
      if (!yes) return
    }
    setBusy(u.name)
    try {
      if (action === 'restart') await api.post('/system/services/restart', { name: u.name })
      else await api.post('/system/services/control', { name: u.name, action })
      notify({ level: 'info', text: `${u.name} ${ACTION_LABEL[action]}`, source: '서비스' })
      // 유닛이 올라오고 상태가 잡힐 틈을 준다
      setTimeout(refresh, 1500)
    } catch (e) {
      notify({ level: 'error', source: '서비스',
        text: e instanceof Error ? e.message : `${u.name} ${action} 실패` })
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
  const noUnitd = units.length > 0 && units.every((u) => !u.controllable)
  const core = units.filter((u) => u.kind !== 'optional')
  const optional = units.filter((u) => u.kind === 'optional')

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between gap-3">
        <div>
          <h2 className="text-lg font-semibold">서비스</h2>
          <p className="mt-1 text-xs text-neutral-400">
            유닛은 <b>기동 시점의 코드</b>로 돕니다. 코드를 고쳤으면 재시작해야 반영됩니다.
            선택 데몬은 여기서 켜고, <b>부팅 시 시작</b>을 고릅니다.
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
      {noUnitd && (
        <p className="rounded border border-neutral-600 bg-neutral-800/60 px-3 py-2
                      text-xs text-neutral-400">
          켜기/끄기는 호스트의 서비스 관리 데몬(piper-unitd)이 실행합니다 — 지금은 없어서
          상태만 보입니다. <code>deploy/install-daemons.sh unitd</code>
        </p>
      )}

      <div className="space-y-1.5">
        {gateway && (
          <Row
            name="게이트웨이 (웹 서버)" active stale={gateway.stale}
            detail={`pid ${gateway.pid} · ${age(gateway.age_s)} 전 기동`}
            busy={busy === 'gateway'}
            restartDisabled={!gateway.restartable}
            onRestart={restartGateway}
          />
        )}
        {core.map((u) => <UnitRow key={u.name} u={u} busy={busy === u.name} onAct={act} />)}
        {optional.length > 0 && (
          <p className="pt-2 text-[11px] uppercase tracking-wide text-neutral-500">선택 데몬</p>
        )}
        {optional.map((u) => <UnitRow key={u.name} u={u} busy={busy === u.name} onAct={act} />)}
        {units.length === 0 && !gateway && (
          <p className="text-xs text-neutral-500">systemd 유닛을 찾지 못했습니다.</p>
        )}
      </div>
    </div>
  )
}

function UnitRow({ u, busy, onAct }: { u: Unit; busy: boolean; onAct: (u: Unit, a: Action) => void }) {
  const detail = !u.installed
    ? '설치 안 됨 — deploy/install-daemons.sh ' + u.name.replace(/^piper-/, '')
    : `${u.active
      ? (u.age_s !== null ? `${age(u.age_s)} 전 기동` : '실행 중 · 버스 하트비트')
      : '멈춤'}${u.pid ? ` · pid ${u.pid}` : ''}`
  const lock = u.readonly ? '안전장치는 웹에서 손대지 않습니다'
    : !u.controllable ? '서비스 관리 데몬(piper-unitd)이 없습니다' : undefined
  return (
    <Row name={u.name} active={u.active} stale={u.stale} detail={detail}
      description={u.description} busy={busy}
      restartDisabled={u.restartable === false || !u.active}
      onRestart={() => onAct(u, 'restart')}
      power={u.installed !== false ? {
        on: u.active, disabled: !u.controllable, title: lock,
        onToggle: () => onAct(u, u.active ? 'stop' : 'start'),
      } : undefined}
      boot={u.enabled === null || u.enabled === undefined ? undefined : {
        on: u.enabled, disabled: !u.controllable, title: lock,
        onToggle: () => onAct(u, u.enabled ? 'disable' : 'enable'),
      }}
    />
  )
}

function Row({ name, active, stale, detail, description, busy, restartDisabled, onRestart, power, boot }: {
  name: string; active: boolean; stale: boolean; detail: string; description?: string
  busy: boolean; restartDisabled?: boolean; onRestart: () => void
  power?: { on: boolean; disabled: boolean; title?: string; onToggle: () => void }
  boot?: { on: boolean; disabled: boolean; title?: string; onToggle: () => void }
}) {
  return (
    <div className={`flex items-center gap-3 rounded border px-3 py-2 ${stale
      ? 'border-amber-500/40 bg-amber-500/5' : 'border-neutral-700 bg-neutral-800'}`}>
      <span className={`h-2 w-2 shrink-0 rounded-full ${active
        ? 'bg-emerald-500' : 'bg-neutral-600'}`} aria-hidden />
      <div className="min-w-0 flex-1">
        <p className="truncate text-sm text-neutral-200">
          {name}
          {description && <span className="ml-2 text-xs text-neutral-500">{description}</span>}
          {stale && <span className="ml-2 text-xs text-amber-400">코드가 더 새것</span>}
        </p>
        <p className="text-[11px] text-neutral-500 tabular-nums">{detail}</p>
      </div>
      {boot && (
        <label className={`flex shrink-0 items-center gap-1 text-[11px] ${boot.disabled
          ? 'text-neutral-600' : 'text-neutral-400'}`} title={boot.title}>
          <input type="checkbox" checked={boot.on} disabled={busy || boot.disabled}
            onChange={boot.onToggle} className="accent-blue-500" />
          부팅 시 시작
        </label>
      )}
      {power && (
        <button onClick={power.onToggle} disabled={busy || power.disabled} title={power.title}
          className={`shrink-0 px-2 py-1 text-xs rounded text-white disabled:bg-neutral-700
            disabled:text-neutral-500 ${power.on
              ? 'bg-neutral-600 hover:bg-red-700' : 'bg-emerald-700 hover:bg-emerald-600'}`}>
          {busy ? '…' : power.on ? '끄기' : '켜기'}
        </button>
      )}
      <button onClick={onRestart} disabled={busy || restartDisabled}
        title={restartDisabled ? '이 방식으로는 재시작할 수 없습니다' : undefined}
        className={`shrink-0 px-2 py-1 text-xs rounded disabled:bg-neutral-700
          disabled:text-neutral-500 text-white ${stale
            ? 'bg-amber-600 hover:bg-amber-500' : 'bg-blue-600 hover:bg-blue-500'}`}>
        {busy ? '…' : '재시작'}
      </button>
    </div>
  )
}
