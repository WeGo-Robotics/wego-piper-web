import { useCallback, useEffect, useState, type ReactNode } from 'react'
import { useSystemMessage } from './SystemMessages'
import { api } from '../services/api'
import { useT, tr, msg } from '../i18n'

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
 *
 * **장치 확장**(feature/pika.md §2): 이미지 주소를 넣으면 unitd 가 받아 `piper-ext-<name>` 유닛으로 켠다.
 * 확장 유닛은 다른 데몬과 같은 줄(켜기/끄기·부팅 시 시작·로그)에 화면 열기·제거가 붙는다. 화면은 **새 창**으로
 * 연다 — 메인 창이 남아 heartbeat·E-stop 이 산다(플러그인 화면과 같다).
 */

type Unit = {
  name: string; active: boolean; since: number | null; stale: boolean
  age_s: number | null; pid: number | null; description: string
  // 컨테이너 배포에서는 버스 생존 키로만 보인다 — 기동 시각을 모르고 재시작도 불가
  restartable?: boolean
  kind?: 'core' | 'optional' | 'extension'
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
type Ext = {
  name: string; title: string; image: string; unit: string; alive: boolean; ui_ready: boolean
  manifest: { ui?: string | null; rpc_hub: string; devices?: string[] }
}
type ExtList = { extensions: Ext[]; registries: string[]; login_enabled: boolean }
type ExtStatus = { state: 'idle' | 'running' | 'done' | 'failed'; image?: string; name?: string; error?: string; lines?: string[] }

function age(sec: number | null): string {
  if (sec === null) return '?'
  if (sec < 60) return tr('{sec}초', { sec })
  if (sec < 3600) return tr('{sec}분', { sec: Math.round(sec / 60) })
  if (sec < 86400) return tr('{sec}시간', { sec: (sec / 3600).toFixed(1) })
  return tr('{sec}일', { sec: (sec / 86400).toFixed(1) })
}

const ACTION_LABEL: Record<Action, string> = {
  start: msg('켜짐'), stop: msg('꺼짐'), restart: msg('재시작됨'), enable: msg('부팅 시 시작'), disable: msg('부팅 시 시작 해제'),
}

export default function ServicesPanel({ onShowLog }: { onShowLog?: (unit: string) => void } = {}) {
  const t = useT()
  const { notify, confirm } = useSystemMessage()
  const [units, setUnits] = useState<Unit[]>([])
  const [gateway, setGateway] = useState<Gateway | null>(null)
  const [busy, setBusy] = useState<string | null>(null)
  const [ext, setExt] = useState<ExtList | null>(null)
  const [extErr, setExtErr] = useState<string | null>(null)

  const refresh = useCallback(() => {
    api.get<{ units: Unit[]; gateway: Gateway }>('/system/services')
      .then((r) => { setUnits(r.units ?? []); setGateway(r.gateway ?? null) })
      .catch(() => {})
    api.get<ExtList>('/system/extensions')
      .then((r) => { setExt(r); setExtErr(null) })
      .catch((e) => { setExt(null); setExtErr(e instanceof Error ? e.message : String(e)) })
  }, [])

  useEffect(() => { refresh() }, [refresh])

  const act = async (u: Unit, action: Action) => {
    // 핵심 데몬을 끄는 건 장치가 사라지는 일이다 — 한 번 묻는다. 논블로킹 모달
    // (window.confirm 은 이벤트 루프를 막아 E-stop heartbeat 가 끊긴다).
    if (action === 'stop' && u.kind !== 'optional') {
      const yes = await confirm(
        t('{name} 을(를) 끕니다.\n\n이 장치(팔·카메라)는 다시 켤 때까지 화면에서 사라집니다.\n부팅 시 시작 설정은 그대로입니다 — 재부팅하면 다시 뜹니다.', { name: u.name }))
      if (!yes) return
    }
    setBusy(u.name)
    try {
      // 확장 유닛은 unitd 를 거쳐야 재시작된다 — 컨테이너의 재시작 폴백(버스 RPC)은 핵심 데몬만 받는다
      if (action === 'restart' && u.kind !== 'extension') await api.post('/system/services/restart', { name: u.name })
      else await api.post('/system/services/control', { name: u.name, action })
      notify({ level: 'info', text: `${u.name} ${t(ACTION_LABEL[action])}`, source: t('서비스') })
      // 유닛이 올라오고 상태가 잡힐 틈을 준다
      setTimeout(refresh, 1500)
    } catch (e) {
      notify({ level: 'error', source: t('서비스'),
        text: e instanceof Error ? e.message : t('{name} {action} 실패', { name: u.name, action }) })
    } finally { setBusy(null) }
  }

  const restartGateway = async () => {
    // ⚠ `window.confirm` 을 쓰지 않는다 — 이벤트 루프를 막아 E-stop heartbeat 가
    //   끊긴다. 논블로킹 모달이다.
    const yes = await confirm(
      t('게이트웨이를 재시작합니다.\n\n몇 초 동안 화면이 끊기고, 그동안 들어온 요청은 실패합니다.\n돌고 있는 학습·정책서버는 별도 유닛이라 영향받지 않습니다.'))
    if (!yes) return
    setBusy('gateway')
    try {
      await api.post('/system/restart', {})
      notify({ level: 'warn', source: t('서비스'),
        text: t('게이트웨이를 재시작하는 중입니다 — 잠시 뒤 새로고침하세요.') })
    } catch (e) {
      notify({ level: 'error', source: t('서비스'),
        text: e instanceof Error ? e.message : t('재시작 실패') })
    } finally { setBusy(null) }
  }

  const staleCount = units.filter((u) => u.stale).length + (gateway?.stale ? 1 : 0)
  const noUnitd = units.length > 0 && units.every((u) => !u.controllable)
  const core = units.filter((u) => u.kind !== 'optional' && u.kind !== 'extension')
  const optional = units.filter((u) => u.kind === 'optional')
  const extUnits = units.filter((u) => u.kind === 'extension')

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between gap-3">
        <div>
          <h2 className="text-lg font-semibold">{t('서비스')}</h2>
          <p className="mt-1 text-xs text-neutral-400">
            {t.rich('유닛은 {b}로 돕니다. 코드를 고쳤으면 재시작해야 반영됩니다. 선택 데몬은 여기서 켜고, {b_2}을 고릅니다.', {
              b: <b>{t('기동 시점의 코드')}</b>,
              b_2: <b>{t('부팅 시 시작')}</b>,
            })}
          </p>
        </div>
        <button onClick={refresh}
          className="px-2 py-1 text-xs rounded bg-neutral-700 hover:bg-neutral-600">
          {t('새로고침')}
        </button>
      </div>

      {staleCount > 0 && (
        <p className="rounded border border-amber-500/40 bg-amber-500/10 px-3 py-2
                      text-xs text-amber-300">
          {t.rich('⚠ {staleCount}개가 {b} 떠 있습니다 — 재시작 전에는 그 변경이 반영되지 않습니다.', {
            staleCount,
            b: <b>{t('고친 코드보다 먼저')}</b>,
          })}
        </p>
      )}
      {noUnitd && (
        <p className="rounded border border-neutral-600 bg-neutral-800/60 px-3 py-2
                      text-xs text-neutral-400">
          {t.rich('켜기/끄기는 호스트의 서비스 관리 데몬(piper-unitd)이 실행합니다 — 지금은 없어서 상태만 보입니다. {code}', {
            code: <code>deploy/install-daemons.sh unitd</code>,
          })}
        </p>
      )}

      <div className="space-y-1.5">
        {gateway && (
          <Row
            name={t('게이트웨이 (웹 서버)')} active stale={gateway.stale}
            detail={t('pid {pid} · {age} 전 기동', { pid: gateway.pid, age: age(gateway.age_s) })}
            busy={busy === 'gateway'}
            restartDisabled={!gateway.restartable}
            onRestart={restartGateway}
          />
        )}
        {core.map((u) => <UnitRow key={u.name} u={u} busy={busy === u.name} onAct={act} onShowLog={onShowLog} />)}
        {optional.length > 0 && (
          <p className="pt-2 text-[11px] uppercase tracking-wide text-neutral-500">{t('선택 데몬')}</p>
        )}
        {optional.map((u) => <UnitRow key={u.name} u={u} busy={busy === u.name} onAct={act} onShowLog={onShowLog} />)}
        {units.length === 0 && !gateway && (
          <p className="text-xs text-neutral-500">{t('systemd 유닛을 찾지 못했습니다.')}</p>
        )}
      </div>

      <Extensions list={ext} err={extErr} units={extUnits} busy={busy} setBusy={setBusy}
        onAct={act} onShowLog={onShowLog} refresh={refresh} />
    </div>
  )
}

/** 장치 확장 — 추가·목록·화면·제거 (feature/pika.md §2). 손은 호스트 unitd 다. */
function Extensions({ list, err, units, busy, setBusy, onAct, onShowLog, refresh }: {
  list: ExtList | null; err: string | null; units: Unit[]; busy: string | null
  setBusy: (b: string | null) => void; onAct: (u: Unit, a: Action) => void
  onShowLog?: (unit: string) => void; refresh: () => void
}) {
  const t = useT()
  const { notify, confirm } = useSystemMessage()
  const [image, setImage] = useState('')
  const [job, setJob] = useState<ExtStatus | null>(null)

  // 추가가 도는 동안만 진행을 읽는다 — 앞 요청이 끝난 뒤에 다음을 보낸다(겹쳐 쌓이지 않게)
  useEffect(() => {
    if (job?.state !== 'running') return
    let alive = true
    const tick = async () => {
      try {
        const s = await api.get<ExtStatus>('/system/extensions/status')
        if (!alive) return
        setJob(s)
        if (s.state === 'done') {
          notify({ level: 'info', source: t('장치 확장'), text: t('{name} 을(를) 추가하고 켰습니다', { name: s.name ?? '' }) })
          refresh()
          return
        }
        if (s.state === 'failed') {
          notify({ level: 'error', source: t('장치 확장'), text: s.error ? t(s.error) : t('추가 실패') })
          return
        }
      } catch { /* 다음 틱에 다시 */ }
      if (alive) setTimeout(tick, 1000)
    }
    const h = setTimeout(tick, 1000)
    return () => { alive = false; clearTimeout(h) }
  }, [job?.state, notify, refresh, t])

  const add = async () => {
    const img = image.trim()
    if (!img) return
    try {
      setJob(await api.post<ExtStatus>('/system/extensions', { image: img }))
    } catch (e) {
      notify({ level: 'error', source: t('장치 확장'), text: e instanceof Error ? e.message : t('추가 실패') })
    }
  }

  const remove = async (e: Ext) => {
    const yes = await confirm(t('{title} 확장을 지웁니다.\n\n유닛을 끄고 지웁니다. 이 확장이 쥔 장치는 화면에서 사라집니다.\n캘리브레이션·배정 데이터는 남깁니다 — 다시 추가하면 그대로 씁니다.', { title: e.title }))
    if (!yes) return
    setBusy(e.unit)
    try {
      await api.delete(`/system/extensions/${encodeURIComponent(e.name)}`, { timeoutMs: 130_000 })
      notify({ level: 'info', source: t('장치 확장'), text: t('{title} 을(를) 지웠습니다', { title: e.title }) })
      refresh()
    } catch (x) {
      notify({ level: 'error', source: t('장치 확장'), text: x instanceof Error ? x.message : t('제거 실패') })
    } finally { setBusy(null) }
  }

  const byUnit = new Map((list?.extensions ?? []).map((e) => [e.unit, e]))
  const running = job?.state === 'running'

  return (
    <div className="space-y-2 pt-2">
      <p className="text-[11px] uppercase tracking-wide text-neutral-500">{t('장치 확장')}</p>
      <p className="text-xs text-neutral-400">
        {t('PIKA 같은 장치를 이미지 하나로 붙입니다 — 이미지 주소를 넣으면 이 기계가 받아서 서비스로 켭니다. sudo 는 필요 없습니다.')}
      </p>
      {list && !list.login_enabled && (
        <p className="rounded border border-amber-500/40 bg-amber-500/10 px-3 py-2 text-xs text-amber-300">
          {t('⚠ 로그인이 꺼져 있습니다 — 이 네트워크의 누구나 확장을 더할 수 있고, 확장은 USB·시리얼 장치 권한을 갖습니다. 설정 → 보안에서 비밀번호를 정하세요.')}
        </p>
      )}
      {err && (
        <p className="rounded border border-neutral-600 bg-neutral-800/60 px-3 py-2 text-xs text-neutral-400">{t(err)}</p>
      )}
      {list && (
        <div className="flex items-center gap-2">
          <input value={image} onChange={(e) => setImage(e.target.value)} disabled={running}
            onKeyDown={(e) => { if (e.key === 'Enter') add() }}
            placeholder={t('이미지 주소 — 예: {example}', { example: `${list.registries[0] ?? ''}pika-docker:0.1.0` })}
            className="min-w-0 flex-1 rounded border border-neutral-700 bg-neutral-900 px-2 py-1 text-sm text-neutral-200" />
          <button onClick={add} disabled={running || !image.trim()}
            className="shrink-0 rounded bg-emerald-700 px-3 py-1 text-xs text-white hover:bg-emerald-600 disabled:bg-neutral-700 disabled:text-neutral-500">
            {running ? t('추가하는 중…') : t('확장 추가')}
          </button>
        </div>
      )}
      {list && (
        <p className="text-[11px] text-neutral-500">
          {t('받을 수 있는 곳: {registries} · 이 기계에 이미 있는 이미지', { registries: list.registries.join(', ') })}
        </p>
      )}
      {job && job.state !== 'idle' && (job.lines?.length || job.error) ? (
        <pre className="max-h-40 overflow-auto rounded bg-neutral-900 p-2 text-[11px] text-neutral-400">
          {(job.lines ?? []).slice(-12).join('\n')}{job.error ? `\n✗ ${t(job.error)}` : ''}
        </pre>
      ) : null}
      <div className="space-y-1.5">
        {units.map((u) => {
          const e = byUnit.get(u.name)
          return (
            <UnitRow key={u.name} u={u} busy={busy === u.name} onAct={onAct} onShowLog={onShowLog}
              extra={e && (
                <>
                  {e.manifest.ui && (
                    <button onClick={() => window.open(`/ext/${e.name}/ui/`, `piper-ext-${e.name}`, 'popup,width=1280,height=820')}
                      disabled={!u.active} title={u.active ? e.image : t('꺼져 있습니다')}
                      className="shrink-0 rounded bg-neutral-700 px-2 py-1 text-xs text-neutral-200 hover:bg-neutral-600 disabled:text-neutral-500">
                      {t('화면')}
                    </button>
                  )}
                  <button onClick={() => remove(e)} disabled={busy === u.name}
                    className="shrink-0 rounded bg-neutral-700 px-2 py-1 text-xs text-neutral-200 hover:bg-red-700">
                    {t('제거')}
                  </button>
                </>
              )} />
          )
        })}
        {list && units.length === 0 && (
          <p className="text-xs text-neutral-500">{t('추가한 확장이 없습니다.')}</p>
        )}
      </div>
    </div>
  )
}

function UnitRow({ u, busy, onAct, onShowLog, extra }: { u: Unit; busy: boolean; onAct: (u: Unit, a: Action) => void; onShowLog?: (unit: string) => void; extra?: ReactNode }) {
  const t = useT()
  const detail = !u.installed
    ? (u.kind === 'extension' ? t('유닛 파일이 없습니다 — 확장을 다시 추가하세요')
      : t('설치 안 됨 — deploy/install-daemons.sh {name}', { name: u.name.replace(/^piper-/, '') }))
    : `${u.active
      ? (u.age_s !== null ? t('{age} 전 기동', { age: age(u.age_s) }) : t('실행 중 · 버스 하트비트'))
      : t('멈춤')}${u.pid ? ` · pid ${u.pid}` : ''}`
  const lock = u.readonly ? t('안전장치는 웹에서 손대지 않습니다')
    : !u.controllable ? t('서비스 관리 데몬(piper-unitd)이 없습니다') : undefined
  return (
    <Row name={u.name} active={u.active} stale={u.stale} detail={detail}
      description={u.description} busy={busy} extra={extra}
      restartDisabled={u.restartable === false || !u.active}
      onRestart={() => onAct(u, 'restart')}
      onLog={onShowLog ? () => onShowLog(u.name.replace(/^piper-/, '')) : undefined}
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

function Row({ name, active, stale, detail, description, busy, restartDisabled, onRestart, onLog, power, boot, extra }: {
  name: string; active: boolean; stale: boolean; detail: string; description?: string; extra?: ReactNode
  busy: boolean; restartDisabled?: boolean; onRestart: () => void; onLog?: () => void
  power?: { on: boolean; disabled: boolean; title?: string; onToggle: () => void }
  boot?: { on: boolean; disabled: boolean; title?: string; onToggle: () => void }
}) {
  const t = useT()
  return (
    <div className={`flex items-center gap-3 rounded border px-3 py-2 ${stale
      ? 'border-amber-500/40 bg-amber-500/5' : 'border-neutral-700 bg-neutral-800'}`}>
      <span className={`h-2 w-2 shrink-0 rounded-full ${active
        ? 'bg-emerald-500' : 'bg-neutral-600'}`} aria-hidden />
      <div className="min-w-0 flex-1">
        <p className="truncate text-sm text-neutral-200">
          {name}
          {description && <span className="ml-2 text-xs text-neutral-500">{description}</span>}
          {stale && <span className="ml-2 text-xs text-amber-400">{t('코드가 더 새것')}</span>}
        </p>
        <p className="text-[11px] text-neutral-500 tabular-nums">{detail}</p>
      </div>
      {extra}
      {onLog && (
        <button onClick={onLog} title={t('이 데몬의 저널')}
          className="shrink-0 px-2 py-1 text-xs rounded bg-neutral-700 hover:bg-neutral-600 text-neutral-200">
          {t('로그')}
        </button>
      )}
      {boot && (
        <label className={`flex shrink-0 items-center gap-1 text-[11px] ${boot.disabled
          ? 'text-neutral-600' : 'text-neutral-400'}`} title={boot.title}>
          <input type="checkbox" checked={boot.on} disabled={busy || boot.disabled}
            onChange={boot.onToggle} className="accent-blue-500" />
          {t('부팅 시 시작')}
        </label>
      )}
      {power && (
        <button onClick={power.onToggle} disabled={busy || power.disabled} title={power.title}
          className={`shrink-0 px-2 py-1 text-xs rounded text-white disabled:bg-neutral-700
            disabled:text-neutral-500 ${power.on
              ? 'bg-neutral-600 hover:bg-red-700' : 'bg-emerald-700 hover:bg-emerald-600'}`}>
          {busy ? '…' : power.on ? t('끄기') : t('켜기')}
        </button>
      )}
      <button onClick={onRestart} disabled={busy || restartDisabled}
        title={restartDisabled ? t('이 방식으로는 재시작할 수 없습니다') : undefined}
        className={`shrink-0 px-2 py-1 text-xs rounded disabled:bg-neutral-700
          disabled:text-neutral-500 text-white ${stale
            ? 'bg-amber-600 hover:bg-amber-500' : 'bg-blue-600 hover:bg-blue-500'}`}>
        {busy ? '…' : t('재시작')}
      </button>
    </div>
  )
}
