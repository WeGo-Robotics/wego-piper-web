import { useCallback, useEffect, useState } from 'react'
import { Link, useNavigate, useParams } from 'react-router-dom'
import { api } from '../services/api'
import { useT } from '../i18n'
import { useSystemMessage } from '../components/SystemMessages'
import { useWebSocket, type WsMessage } from '../hooks/useWebSocket'
import { useLiveFrames } from '../hooks/useCameraLive'
import LogViewer from '../components/LogViewer'
import LiveCameraFrame from '../components/LiveCameraFrame'
import PluginConfigForm from '../components/PluginConfigForm'
import { openPluginUi, PluginStateDot, ScopeBadges } from '../components/PluginBits'
import { isRunning, type PluginConfigValues, type PluginDeps, type PluginInfo, type PluginRun, type PluginTopic } from '../types/plugins'

const TABS = [
  { id: 'config', label: '설정' },
  { id: 'output', label: '출력' },
  { id: 'log', label: '로그' },
] as const
type TabId = (typeof TABS)[number]['id']

type DetPayload = { ts: number; objects: { label: string; conf: number }[]; text: string; publisher?: string }

// 출력 탭의 실시간 원본(카메라 슬롯의 shm). 카메라는 15fps 로 열려 있다(수집 기본) — 서버 기본 10 이면 장면이
// 듬성듬성하다. 한 화면에 한두 대라 인코딩은 작다
const LIVE_FPS = 15

/**
 * 플러그인 상세 — 제목 옆 탭 (feature/plugins.md §8-1). 설정은 도는 중 읽기 전용이고 값은
 * 서버가 플러그인별로 저장해 다음 시작에 재사용한다. "UI 보기" 는 새 창(§8-2).
 */
export default function PluginDetailPage() {
  const t = useT()
  const { name = '' } = useParams()
  const navigate = useNavigate()
  const { notify, confirm: ask } = useSystemMessage()
  const [info, setInfo] = useState<PluginInfo | null>(null)
  const [tab, setTab] = useState<TabId>('output')
  const [config, setConfig] = useState<PluginConfigValues>({})
  const [cameras, setCameras] = useState<Record<string, string>>({})
  const [arms, setArms] = useState<Record<string, string>>({})
  const [inputs, setInputs] = useState<Record<string, string>>({})
  const [topics, setTopics] = useState<Record<string, PluginTopic>>({})
  const [dirty, setDirty] = useState(false)
  const [busy, setBusy] = useState(false)
  const [logs, setLogs] = useState<string[]>([])
  const [detections, setDetections] = useState<Record<string, DetPayload>>({})
  const [runs, setRuns] = useState<PluginRun[]>([])
  const [deps, setDeps] = useState<PluginDeps | null>(null)

  const fail = (text: string) => notify({ level: 'error', text, source: t('플러그인') })

  const load = useCallback(async () => {
    try {
      const r = await api.get<PluginInfo>(`/plugins/${name}`)
      setInfo(r)
      if (r.manifest?.outputs.detections.length) {
        const all = await api.get<Record<string, DetPayload>>('/vision/detections')
        setDetections(Object.fromEntries(Object.entries(all).filter(([k, v]) => r.manifest!.outputs.detections.includes(k) || v?.publisher === name)))
      }
      if (r.manifest?.mission) setRuns((await api.get<{ runs: PluginRun[] }>(`/plugins/${name}/runs`)).runs)
      if (r.manifest?.outputs.topics.length) {
        const got: Record<string, PluginTopic> = {}
        for (const tp of r.manifest.outputs.topics) {
          try { got[tp] = await api.get<PluginTopic>(`/plugins/topics/${tp}`) } catch { /* 아직 없다 */ }
        }
        setTopics(got)
      }
      if (r.manifest && r.manifest.python !== 'system') setDeps(await api.get<PluginDeps>(`/plugins/${name}/deps`))
    } catch (e) {
      if (!info) fail(e instanceof Error ? e.message : t('불러오기 실패'))
    }
  }, [name, info === null]) // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    void load()
    const id = setInterval(() => { void load() }, 1500)
    return () => clearInterval(id)
  }, [load])

  // 저장된 설정을 폼의 초깃값으로 — 사람이 고치기 시작한 뒤에는 덮어쓰지 않는다
  useEffect(() => {
    if (!info || dirty) return
    const defaults: PluginConfigValues = {}
    for (const f of info.manifest?.config ?? []) if (f.default !== undefined && f.default !== null) defaults[f.key] = f.default
    setConfig({ ...defaults, ...(info.saved?.config ?? {}) })
    setCameras(info.saved?.cameras ?? {})
    setArms(info.saved?.arms ?? {})
    const inDefaults: Record<string, string> = {}
    for (const s of info.manifest?.inputs ?? []) if (s.source) inDefaults[s.key] = s.source
    setInputs({ ...inDefaults, ...(info.saved?.inputs ?? {}) })
    if (info.log_tail && logs.length === 0) setLogs(info.log_tail)
  }, [info, dirty]) // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    if (info?.manifest?.mission) setTab((cur) => cur) // 미션도 출력이 기본 — 저널은 출력 탭 안
  }, [info?.manifest?.mission])

  useWebSocket('/ws', {
    onMessage: useCallback((msg: WsMessage) => {
      if (msg.type === 'plugin_log' && msg.name === name) {
        setLogs((prev) => { const next = [...prev, msg.data]; return next.length > 500 ? next.slice(-500) : next })
      } else if (msg.type === 'plugin_state' && msg.name === name) {
        void load()
      }
    }, [name, load]),
  })

  const running = isRunning(info?.state)
  const m = info?.manifest

  const save = async () => {
    await api.put(`/plugins/${name}/config`, { config, cameras, inputs, arms })
    setDirty(false)
  }
  const start = async () => {
    setBusy(true)
    try {
      if (dirty) await save()
      setLogs([])
      await api.post(`/plugins/${name}/start`, { config, cameras, inputs, arms }, { timeoutMs: 60000 })
      setTab('output'); await load()
    } catch (e) { fail(e instanceof Error ? e.message : t('시작 실패')) } finally { setBusy(false) }
  }
  const stop = async () => {
    setBusy(true)
    try { await api.post(`/plugins/${name}/stop`); await load() }
    catch (e) { fail(e instanceof Error ? e.message : t('정지 실패')) } finally { setBusy(false) }
  }
  const restart = async () => { await stop(); await start() }
  const remove = async () => {
    const q = info?.instance
      ? t('인스턴스 {name} 을 지울까? 저장된 설정이 사라진다 (코드는 남는다)', { name })
      : t('플러그인 {name} 을 지울까? 폴더와 저장된 설정이 사라진다', { name })
    if (!(await ask(q, { danger: true }))) return
    try { await api.delete(`/plugins/${name}`); navigate('/plugins') }
    catch (e) { fail(e instanceof Error ? e.message : t('삭제 실패')) }
  }
  const installDeps = async () => {
    try { setDeps(await api.post<PluginDeps>(`/plugins/${name}/deps`)); setTab('log') }
    catch (e) { fail(e instanceof Error ? e.message : t('의존 설치 실패')) }
  }
  const command = async (cmd: string) => {
    try { await api.post(`/plugins/${name}/command`, { name: cmd, args: {} }) }
    catch (e) { fail(e instanceof Error ? e.message : t('명령 실패')) }
  }

  const stateRows = m?.outputs.state ?? []
  const kv = info?.kv ?? {}
  // 캠 화면이 있으면 상태는 절반 폭 열로 간다 — 긴 값(JSON)은 자르지 않고 줄을 바꾼다
  const hasPreview = (m?.outputs.previews.length ?? 0) > 0
  const valueCls = hasPreview ? 'break-all' : 'truncate'
  const extraKv = Object.entries(kv).filter(([k]) => !stateRows.some((s) => s.key === k) && !['ts', 'uptime_s'].includes(k))
  // 카메라 슬롯 → 스트림 이름. 도는 중이면 그 실행의 매핑, 아니면 저장값 — `/api/cameras/live` 는 이름을 그대로 받는다
  const camMap = info?.cameras ?? info?.saved?.cameras ?? {}
  const liveCams = (m?.cameras ?? []).flatMap((c) => (camMap[c.key] ? [{ key: c.key, stream: camMap[c.key] }] : []))
  // 검출 표시(판단할 때마다 한 장)와 실시간 원본을 같이 본다 — 출력 탭을 보는 동안만 연결을 쥔다
  const live = useLiveFrames(hasPreview && tab === 'output' ? liveCams.map((c) => c.stream) : [], LIVE_FPS)

  return (
    <div className="space-y-6">
      <div className="flex items-center gap-4">
        <h1 className="text-2xl font-bold">{m?.title || name}{info?.instance ? <span className="ml-2 font-mono text-lg text-neutral-400">: {info.instance}</span> : null}</h1>
        {TABS.map((tb) => (
          <button key={tb.id} onClick={() => setTab(tb.id)}
            className={`-mb-px border-b-2 px-3 py-1.5 text-sm transition-colors ${
              tab === tb.id ? 'border-blue-500 text-white' : 'border-transparent text-neutral-400 hover:text-neutral-200'}`}>
            {t(tb.label)}
          </button>
        ))}
        <span className="flex items-center gap-2 text-sm text-neutral-400">
          {info && <PluginStateDot state={info.state} error={!!info.error} />}
          <span className="font-mono text-xs">{info?.state ?? '…'}{info?.pid ? ` · pid ${info.pid}` : ''}</span>
        </span>
        <span className="flex items-center gap-2 ml-2">
          {running ? (
            <>
              <button onClick={() => void stop()} disabled={busy}
                className="px-3 py-1 text-sm rounded bg-neutral-700 hover:bg-red-600 text-white disabled:opacity-50">{t('정지')}</button>
              <button onClick={() => void restart()} disabled={busy}
                className="px-3 py-1 text-sm rounded bg-neutral-700 hover:bg-neutral-600 text-neutral-200 disabled:opacity-50">{t('재시작')}</button>
            </>
          ) : (
            <button onClick={() => void start()} disabled={busy || !!info?.error}
              className="px-3 py-1 text-sm rounded bg-neutral-700 hover:bg-green-600 text-white disabled:opacity-50">{t('시작')}</button>
          )}
          {m?.ui && (
            <button onClick={() => openPluginUi(name, m.ui)}
              className="px-3 py-1 text-sm rounded bg-neutral-700 hover:bg-neutral-600 text-neutral-200">{t('UI 보기')}</button>
          )}
          {m?.commands.map((c) => (
            <button key={c} onClick={() => void command(c)} disabled={!running}
              className="px-2 py-1 text-xs rounded bg-indigo-900/60 hover:bg-indigo-700 text-indigo-100 disabled:opacity-40 font-mono">{c}</button>
          ))}
        </span>
        <Link to="/plugins" className="ml-auto text-sm text-neutral-500 hover:text-neutral-300">← {t('목록')}</Link>
      </div>

      {info?.error && (
        <div className="rounded border border-red-800 bg-red-950/40 p-3 text-sm text-red-300 break-all">{info.error}</div>
      )}
      {m && (
        <div className="flex items-center gap-3 flex-wrap text-xs text-neutral-500">
          <span className="font-mono">{name} · v{m.version}{info?.bundled ? ` · ${t('번들')}` : ''}</span>
          {m.resources.gpu && <span className="px-1.5 py-0.5 rounded bg-amber-900/60 text-amber-300">GPU</span>}
          {m.mission && <span className="px-1.5 py-0.5 rounded bg-indigo-900/60 text-indigo-300">{t('미션')}</span>}
          <ScopeBadges permissions={m.permissions} />
          {info?.state === 'error' && <span className="text-red-400">{t('마지막 실행이 실패했다 — 로그 탭을 보라')}</span>}
          {(info?.instance || !info?.bundled) && !running && (
            <button onClick={() => void remove()} className="ml-auto text-neutral-600 hover:text-red-400">{info?.instance ? t('인스턴스 삭제') : t('삭제')}</button>
          )}
        </div>
      )}
      {m?.description && <p className="text-sm text-neutral-400">{m.description}</p>}

      {tab === 'config' && m && (
        <section className="rounded-lg border border-neutral-700 bg-neutral-800 p-4 space-y-3">
          {running && <p className="text-xs text-amber-300">{t('도는 중에는 읽기 전용이다 — 바꾸려면 정지한 뒤 고치고 다시 시작하라')}</p>}
          <PluginConfigForm fields={m.config} cameraSlots={m.cameras} armSlots={m.arms ?? []} inputSlots={m.inputs} values={config} cameras={cameras} arms={arms} inputs={inputs} disabled={running}
            onChange={(k, v) => { setConfig((c) => ({ ...c, [k]: v })); setDirty(true) }}
            onCameraChange={(slot, stream) => { setCameras((c) => ({ ...c, [slot]: stream })); setDirty(true) }}
            onArmChange={(slot, iface) => { setArms((c) => ({ ...c, [slot]: iface })); setDirty(true) }}
            onInputChange={(slot, source) => { setInputs((c) => ({ ...c, [slot]: source })); setDirty(true) }} />
          {info?.inputs_live && Object.values(info.inputs_live).some((v) => !v) && (
            <p className="text-xs text-amber-300">{t('입력 소스가 지금 살아 있지 않다 — 그 출력을 내는 플러그인(검출기 등)을 먼저 켜라. 막지는 않는다')}</p>
          )}
          {m.config.length === 0 && m.cameras.length === 0 && !(m.arms ?? []).length && <p className="text-xs text-neutral-500">{t('설정 항목이 없다')}</p>}
          {m.python !== 'system' && (
            <div className="rounded border border-neutral-700 bg-neutral-900/60 p-3 text-sm space-y-2">
              <div className="flex items-center gap-3 flex-wrap">
                <span className="font-mono text-xs text-neutral-400">python: {m.python}</span>
                <span className={`text-xs ${deps?.venv_ready ? 'text-green-300' : 'text-amber-300'}`}>
                  {deps?.installing ? t('의존 설치 중…') : deps?.venv_ready ? t('의존 준비됨') : t('의존 설치가 필요하다 — 시작이 거부된다')}
                </span>
                {deps && !deps.requirements && <span className="text-xs text-neutral-500">{t('requirements.txt 없음 — venv 만 만든다')}</span>}
                <button onClick={() => void installDeps()} disabled={running || !!deps?.installing}
                  className="ml-auto px-3 py-1 text-sm rounded bg-neutral-700 hover:bg-blue-600 text-neutral-200 disabled:opacity-50">
                  {deps?.venv_ready ? t('의존 다시 설치') : t('의존 설치')}
                </button>
              </div>
              {m.python === 'venv-isolated' && (
                <p className="text-xs text-neutral-500">{t('venv-isolated 는 게이트웨이 패키지를 상속하지 않는다 — torch 까지 다시 깐다 (수 GB, 수 분)')}</p>
              )}
              {!!m.resources.memory || !!m.resources.cpu ? (
                <p className="text-xs text-neutral-500">
                  {t('자원 제한')}: {m.resources.memory ? `MemoryMax=${m.resources.memory} ` : ''}{m.resources.cpu ? `CPUQuota=${m.resources.cpu}` : ''}
                  {info?.limits_applied === false && ` — ${t('자식 프로세스 폴백에서는 걸리지 않는다')}`}
                </p>
              ) : null}
            </div>
          )}
          {!running && (
            <div className="flex gap-2">
              <button onClick={() => void save().catch((e) => fail(e instanceof Error ? e.message : t('저장 실패')))} disabled={!dirty}
                className="px-3 py-1 text-sm rounded bg-neutral-700 hover:bg-blue-600 text-neutral-200 disabled:opacity-50">{t('저장')}</button>
              <span className="text-xs text-neutral-500 self-center">{t('시작을 누르면 저장도 같이 된다')}</span>
            </div>
          )}
        </section>
      )}

      {tab === 'output' && m && (
        // 캠 화면은 왼쪽, 설명(상태·검출·저널)은 오른쪽 — 좁은 화면에서는 위아래로 쌓인다
        <section className={hasPreview ? 'grid items-start gap-4 xl:grid-cols-2' : 'space-y-4'}>
          {hasPreview && (
            <div className="space-y-3">
              {m.outputs.previews.map((p) => (
                <div key={p} className="rounded-lg border border-neutral-700 bg-black overflow-hidden">
                  <PreviewImage name={p.replace(/^det_/, '')} alt={p} />
                  <div className="px-2 py-1 text-xs text-neutral-500">
                    {t('검출 표시')} <span className="font-mono">· {p}</span>
                  </div>
                </div>
              ))}
              {liveCams.map((c) => (
                <div key={c.key} className="rounded-lg border border-neutral-700 bg-black overflow-hidden">
                  <LiveCameraFrame id={c.stream} alt={c.key} attach={live.attach} className="w-full aspect-[4/3] object-contain" />
                  <div className="px-2 py-1 text-xs text-neutral-500">
                    {t('실시간 원본')} <span className="font-mono">· {c.key} · {c.stream}</span>
                  </div>
                </div>
              ))}
            </div>
          )}
          <div className="min-w-0 space-y-4">
            {(stateRows.length > 0 || extraKv.length > 0) && (
              <div className="rounded-lg border border-neutral-700 bg-neutral-800 p-4">
                <h2 className="text-sm font-semibold text-neutral-300 mb-2">{t('상태')}</h2>
                {Object.keys(kv).length === 0 ? (
                  <p className="text-xs text-neutral-500">{running ? t('플러그인이 아직 상태를 올리지 않았다') : t('꺼져 있다')}</p>
                ) : (
                  <dl className={`grid grid-cols-2 gap-2 text-sm ${hasPreview ? '' : 'md:grid-cols-4'}`}>
                    {stateRows.map((s) => (
                      <div key={s.key} className="min-w-0"><dt className="text-xs text-neutral-500">{s.label || s.key}</dt>
                        <dd className={`font-mono text-neutral-100 ${valueCls}`}>{fmtKv(kv[s.key])}{s.unit ? ` ${s.unit}` : ''}</dd></div>
                    ))}
                    {extraKv.map(([k, v]) => (
                      <div key={k} className="min-w-0"><dt className="text-xs text-neutral-500">{k}</dt><dd className={`font-mono text-neutral-300 ${valueCls}`}>{fmtKv(v)}</dd></div>
                    ))}
                  </dl>
                )}
              </div>
            )}
            {m.outputs.detections.length > 0 && (
              <div className="rounded-lg border border-neutral-700 bg-neutral-800 p-4">
                <h2 className="text-sm font-semibold text-neutral-300 mb-2">{t('검출')}</h2>
                {Object.keys(detections).length === 0 ? <p className="text-xs text-neutral-500">{t('살아 있는 검출이 없다')}</p> : (
                  <ul className="text-sm space-y-1">
                    {Object.entries(detections).map(([k, d]) => (
                      <li key={k} className="font-mono text-xs text-neutral-300 truncate">{d.text}</li>
                    ))}
                  </ul>
                )}
              </div>
            )}
            {m.outputs.topics.length > 0 && (
              <div className="rounded-lg border border-neutral-700 bg-neutral-800 p-4">
                <h2 className="text-sm font-semibold text-neutral-300 mb-2">{t('토픽 출력')}</h2>
                <ul className="text-xs space-y-1">
                  {m.outputs.topics.map((tp) => {
                    const v = topics[tp]
                    return (
                      <li key={tp} className="flex gap-3 items-start">
                        <span className="font-mono text-neutral-400 shrink-0">{tp}{v ? ` #${v.seq}` : ''}</span>
                        <span className="font-mono text-neutral-300 truncate">{v ? JSON.stringify(v.data) : t('아직 없다')}</span>
                      </li>
                    )
                  })}
                </ul>
              </div>
            )}
            {m.mission && (
              <div className="rounded-lg border border-neutral-700 bg-neutral-800 p-4">
                <h2 className="text-sm font-semibold text-neutral-300 mb-2">{t('회차 저널')}</h2>
                {runs.length === 0 ? <p className="text-xs text-neutral-500">{t('아직 저널이 없다')}</p> : (
                  <ul className="text-xs font-mono text-neutral-400 space-y-0.5">
                    {runs.slice(0, 10).map((r) => (
                      <li key={r.id} className="flex gap-3">
                        <span className={r.live ? 'text-green-300' : ''}>{r.id}</span>
                        <span className="text-neutral-600">{Math.round(r.bytes / 1024)} KB</span>
                        <a className="text-blue-400 hover:underline" href={`/api/plugins/${name}/runs/${r.id}`} target="_blank" rel="noreferrer">JSON</a>
                      </li>
                    ))}
                  </ul>
                )}
              </div>
            )}
            {stateRows.length === 0 && !hasPreview && m.outputs.detections.length === 0 && m.outputs.topics.length === 0 && !m.mission && extraKv.length === 0 && (
              <p className="text-xs text-neutral-500">{t('이 플러그인은 화면에 보일 출력을 선언하지 않았다 (plugin.yaml 의 outputs)')}</p>
            )}
          </div>
        </section>
      )}

      {tab === 'log' && (
        <LogViewer logs={logs} onClear={() => setLogs([])} placeholder={t('로그가 없다 — 시작하면 여기로 흐른다')} />
      )}
    </div>
  )
}

// 검출 표시를 다시 부르는 간격 — 한 장이 뜬 뒤 이만큼 쉬고 다음 장. 플러그인이 초당 3~5장 올리니 그보다 촘촘하면 된다
const PREVIEW_GAP_MS = 150
const PREVIEW_RETRY_MS = 1000

/**
 * 플러그인의 검출 표시 — **한 장이 뜨면 다음 장을 부른다**(플러그인이 올리는 속도 그대로).
 * 예전에는 상태 폴링(1.5초)마다 한 장이라, 사과 분류가 초당 3.5장을 올려도 화면은 0.7장이었다.
 * 요청은 늘 하나만 떠 있다 — 느려도 쌓이지 않는다(요청 적체로 버튼이 마비된 적이 있다). 탭이 가려지면 쉰다.
 *
 * 다음 장은 화면 밖(`Image`)에서 받고 다 받은 뒤에만 바꿔 낀다 — 미리보기 키가 잠깐 비면(버스 TTL 3초)
 * 깨진 그림이 깜빡였다. 못 받으면 마지막 장을 흐리게 남긴다.
 */
function PreviewImage({ name, alt }: { name: string; alt: string }) {
  const [src, setSrc] = useState<string | null>(null)
  const [stale, setStale] = useState(false)
  useEffect(() => {
    let dead = false
    let timer: ReturnType<typeof setTimeout> | undefined
    const load = () => {
      if (dead) return
      if (document.visibilityState === 'hidden') { timer = setTimeout(load, PREVIEW_RETRY_MS); return }
      const url = `/api/vision/preview/${encodeURIComponent(name)}?t=${Date.now()}`
      const img = new Image()
      img.onload = () => { if (dead) return; setSrc(url); setStale(false); timer = setTimeout(load, PREVIEW_GAP_MS) }
      img.onerror = () => { if (dead) return; setStale(true); timer = setTimeout(load, PREVIEW_RETRY_MS) }
      img.src = url
    }
    load()
    return () => { dead = true; clearTimeout(timer) }
  }, [name])
  if (!src) return <div className="w-full aspect-[4/3]" aria-label={alt} />
  return <img src={src} alt={alt} className="w-full" style={{ opacity: stale ? 0.3 : 1 }} />
}

function fmtKv(v: unknown): string {
  if (v === null || v === undefined) return '—'
  if (typeof v === 'number') return Number.isInteger(v) ? String(v) : v.toFixed(2)
  if (typeof v === 'object') return JSON.stringify(v)
  return String(v)
}
