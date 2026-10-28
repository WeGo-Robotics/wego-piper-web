import { useCallback, useEffect, useRef, useState } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { api } from '../services/api'
import { tb, useT } from '../i18n'
import { useSystemMessage } from '../components/SystemMessages'
import { openPluginUi, PluginStateDot, ScopeBadges } from '../components/PluginBits'
import { isRunning, type PluginInfo } from '../types/plugins'

/**
 * 플러그인 목록 — 카드 그리드 (feature/plugins.md §8-1).
 *
 * 카드는 켜고 끄는 것까지만 한다. 설정·출력·로그는 상세(`/plugins/<name>`)에서 탭으로.
 * 설정 폼을 카드에 펼치지 않는다 — 카드가 길어지면 목록이 목록 노릇을 못 한다.
 */
export default function PluginsPage() {
  const t = useT()
  const navigate = useNavigate()
  const { notify, confirm: ask } = useSystemMessage()
  const [plugins, setPlugins] = useState<PluginInfo[]>([])
  const [dirs, setDirs] = useState<string[]>([])
  const [busy, setBusy] = useState<string | null>(null)
  const [installPath, setInstallPath] = useState('')
  const [newLabel, setNewLabel] = useState<Record<string, string>>({})
  const fileRef = useRef<HTMLInputElement>(null)

  const load = useCallback(async () => {
    try {
      const r = await api.get<{ plugins: PluginInfo[]; dirs: string[] }>('/plugins')
      setPlugins(r.plugins); setDirs(r.dirs)
    } catch { /* 다음 폴에 회복 */ }
  }, [])
  useEffect(() => {
    void load()
    const id = setInterval(() => { void load() }, 2000)
    return () => clearInterval(id)
  }, [load])

  const fail = (text: string) => notify({ level: 'error', text, source: t('플러그인') })

  const startSaved = (name: string, saved: PluginInfo['saved']) =>
    api.post(`/plugins/${name}/start`, { config: saved.config, cameras: saved.cameras, inputs: saved.inputs ?? {}, arms: saved.arms ?? {} }, { timeoutMs: 60000 })

  const toggle = async (p: PluginInfo) => {
    setBusy(p.name)
    try {
      if (isRunning(p.state)) await api.post(`/plugins/${p.name}/stop`)
      else await startSaved(p.name, p.saved)
      await load()
    } catch (e) {
      fail(e instanceof Error ? e.message : t('실패'))
    } finally { setBusy(null) }
  }

  /**
   * 인스턴스 줄의 시작/정지 — 저장된 설정으로 (상세에서 고친다). 목록의 인스턴스 줄에는 `saved` 가
   * 없어서 시작 직전에 그 인스턴스를 묻는다 — 빈 본문을 보내면 매니페스트 기본값으로 돈다.
   */
  const toggleInstance = async (iid: string, running: boolean) => {
    setBusy(iid)
    try {
      if (running) await api.post(`/plugins/${iid}/stop`)
      else await startSaved(iid, (await api.get<PluginInfo>(`/plugins/${iid}`)).saved)
      await load()
    } catch (e) { fail(e instanceof Error ? e.message : t('실패')) } finally { setBusy(null) }
  }

  const addInstance = async (base: string) => {
    const label = (newLabel[base] ?? '').trim()
    if (!label) return
    try {
      await api.post(`/plugins/${base}/instances`, { label })
      setNewLabel((m) => ({ ...m, [base]: '' })); await load()
    } catch (e) { fail(e instanceof Error ? e.message : t('인스턴스 추가 실패')) }
  }

  /**
   * 설치 — 이미 있으면(409) 덮어쓸지 묻고 같은 것을 `replace` 로 다시 보낸다(업데이트).
   * 저장 설정·실행 기록·인스턴스·venv 는 폴더 밖이라 남는다 (feature/plugins.md §3-2).
   * ⚠ 묻는 것은 논블로킹 모달이다 — `window.confirm` 은 heartbeat 를 끊는다.
   */
  const install = async (send: (replace: boolean) => Promise<PluginInfo>): Promise<boolean> => {
    setBusy('install')
    try {
      let r: PluginInfo
      try {
        r = await send(false)
      } catch (e) {
        if ((e as { status?: number }).status !== 409) throw e
        const ok = await ask(t('{detail} — 새 코드로 업데이트할까? 저장된 설정·실행 기록·인스턴스는 그대로 남는다',
          { detail: (e as Error).message }), { danger: false })
        if (!ok) return false
        r = await send(true)
      }
      const u = r.updated
      notify({
        level: 'info', source: t('플러그인'),
        text: u ? t('플러그인 업데이트됨: {name} v{from} → v{to}', { name: r.name, from: u.from ?? '?', to: u.to })
          : t('플러그인 설치됨: {name}', { name: r.name }),
      })
      if (u?.deps_reset) notify({ level: 'warn', source: t('플러그인'),
        text: t('{name} 의 의존이 바뀌었다 — 상세에서 [의존 설치] 를 다시 해야 시작된다', { name: r.name }) })
      await load()
      return true
    } catch (e) {
      fail(e instanceof Error ? e.message : t('설치 실패'))
      return false
    } finally { setBusy(null) }
  }

  const installFromPath = async () => {
    const path = installPath.trim()
    if (!path) return
    if (await install((replace) => api.post<PluginInfo>('/plugins/install', { path, replace }))) setInstallPath('')
  }

  const installZip = async (file: File) => {
    await install(async (replace) => {
      const res = await fetch(`/api/plugins/install/zip${replace ? '?replace=true' : ''}`, { method: 'POST', body: file })
      const body = await res.json().catch(() => ({}))
      if (!res.ok) throw Object.assign(new Error(tb((body as { detail?: string }).detail) || `HTTP ${res.status}`), { status: res.status })
      return body as PluginInfo
    })
    if (fileRef.current) fileRef.current.value = ''
  }

  const gpuRunning = plugins.filter((p) => p.manifest?.resources.gpu && isRunning(p.state)).map((p) => p.name)

  return (
    <div className="space-y-6">
      <div className="flex items-center gap-4">
        <h1 className="text-2xl font-bold">{t('플러그인')}</h1>
        <span className="text-xs text-neutral-500">{t('폴더 하나로 얹는 파이썬 — 설계·계약은 plugins/README.md')}</span>
      </div>

      {/* 설치 — zip 업로드 / 로컬 폴더 경로. git URL 은 받지 않는다 (§3-2) */}
      <section className="rounded-lg border border-neutral-700 bg-neutral-800 p-3 flex items-center gap-3 flex-wrap text-sm">
        <span className="text-neutral-400">{t('설치')}</span>
        <input type="text" value={installPath} onChange={(e) => setInstallPath(e.target.value)}
          placeholder={t('플러그인 폴더 경로 (이 기계)')}
          className="flex-1 min-w-[16rem] px-2 py-1 rounded bg-neutral-900 border border-neutral-700 text-neutral-100" />
        <button onClick={() => void installFromPath()} disabled={busy !== null || !installPath.trim()}
          className="px-3 py-1 rounded bg-neutral-700 hover:bg-blue-600 text-neutral-200 disabled:opacity-50">{t('폴더로 설치')}</button>
        <input ref={fileRef} type="file" accept=".zip" className="hidden"
          onChange={(e) => { const f = e.target.files?.[0]; if (f) void installZip(f) }} />
        <button onClick={() => fileRef.current?.click()} disabled={busy !== null}
          className="px-3 py-1 rounded bg-neutral-700 hover:bg-blue-600 text-neutral-200 disabled:opacity-50">{t('zip 업로드')}</button>
        <span className="text-xs text-neutral-600 w-full">{t('읽는 폴더: {dirs}', { dirs: dirs.join(' · ') })}</span>
      </section>

      {gpuRunning.length > 1 && (
        <div className="rounded border border-amber-700 bg-amber-950/40 px-3 py-2 text-sm text-amber-300">
          {t('GPU 를 쓰는 플러그인이 둘 이상 돌고 있다 ({names}) — 메모리를 다툴 수 있다. 막지는 않는다', { names: gpuRunning.join(', ') })}
        </div>
      )}

      {plugins.length === 0 ? (
        <p className="text-sm text-neutral-500">{t('플러그인이 없다 — 위에서 설치하거나 번들 폴더(plugins/)를 확인하라')}</p>
      ) : (
        <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-3">
          {plugins.map((p) => {
            const m = p.manifest
            const running = isRunning(p.state)
            const kvPreview = m?.outputs.state.slice(0, 2).map((s) => [s.label || s.key, p.kv?.[s.key]] as const)
              .filter(([, v]) => v !== undefined && v !== null) ?? []
            return (
              <div key={p.name} className={`rounded-lg border bg-neutral-800 p-4 space-y-2 ${p.error ? 'border-red-800' : 'border-neutral-700'}`}>
                <div className="flex items-start gap-2">
                  <PluginStateDot state={p.state} error={!!p.error} />
                  <div className="min-w-0 flex-1">
                    <Link to={`/plugins/${p.name}`} className="font-semibold text-neutral-100 hover:text-blue-300 truncate block">
                      {m?.title || p.name}
                    </Link>
                    <div className="text-xs text-neutral-500 font-mono">{p.name} · v{m?.version ?? '?'}{p.bundled ? ` · ${t('번들')}` : ''}</div>
                  </div>
                  {m?.resources.gpu && <span className="text-[10px] px-1.5 py-0.5 rounded bg-amber-900/60 text-amber-300">GPU</span>}
                  {m?.mission && <span className="text-[10px] px-1.5 py-0.5 rounded bg-indigo-900/60 text-indigo-300">{t('미션')}</span>}
                </div>
                {p.error ? (
                  <p className="text-xs text-red-300 break-all">{p.error}</p>
                ) : (
                  <p className="text-xs text-neutral-400 line-clamp-2">{m?.description}</p>
                )}
                {m && <ScopeBadges permissions={m.permissions} />}
                {kvPreview.length > 0 && (
                  <div className="text-xs text-neutral-300 flex gap-3 flex-wrap">
                    {kvPreview.map(([k, v]) => <span key={String(k)}><span className="text-neutral-500">{k}</span> {String(v)}</span>)}
                  </div>
                )}
                {m?.multi_instance && (
                  <div className="rounded border border-neutral-700/70 divide-y divide-neutral-700/50 text-xs">
                    {(p.instances ?? []).map((inst) => {
                      const r = isRunning(inst.state)
                      return (
                        <div key={inst.name} className="flex items-center gap-2 px-2 py-1">
                          <PluginStateDot state={inst.state} />
                          <Link to={`/plugins/${inst.name}`} className="font-mono text-neutral-200 hover:text-blue-300 truncate">
                            {inst.instance ?? t('기본')}
                          </Link>
                          <span className="text-neutral-600 truncate">{inst.kv?.phase ? String(inst.kv.phase) : inst.state}</span>
                          <button onClick={() => void toggleInstance(inst.name, r)} disabled={busy !== null}
                            className={`ml-auto px-2 py-0.5 rounded text-white disabled:opacity-50 ${r ? 'bg-neutral-700 hover:bg-red-600' : 'bg-neutral-700 hover:bg-green-600'}`}>
                            {r ? t('정지') : t('시작')}
                          </button>
                        </div>
                      )
                    })}
                    <div className="flex items-center gap-2 px-2 py-1">
                      <input type="text" value={newLabel[p.name] ?? ''} placeholder={t('새 인스턴스 라벨 (예: cam2)')}
                        onChange={(e) => setNewLabel((mm) => ({ ...mm, [p.name]: e.target.value }))}
                        onKeyDown={(e) => { if (e.key === 'Enter') void addInstance(p.name) }}
                        className="flex-1 px-2 py-0.5 rounded bg-neutral-900 border border-neutral-700 text-neutral-100 font-mono" />
                      <button onClick={() => void addInstance(p.name)} disabled={!(newLabel[p.name] ?? '').trim()}
                        className="px-2 py-0.5 rounded bg-neutral-700 hover:bg-blue-600 text-neutral-200 disabled:opacity-50">+ {t('인스턴스')}</button>
                    </div>
                  </div>
                )}
                <div className="flex items-center gap-2 pt-1">
                  <button onClick={() => void toggle(p)} disabled={busy !== null || !!p.error}
                    className={`px-3 py-1 text-sm rounded text-white disabled:opacity-50 ${running ? 'bg-neutral-700 hover:bg-red-600' : 'bg-neutral-700 hover:bg-green-600'}`}>
                    {running ? t('정지') : t('시작')}
                  </button>
                  <button onClick={() => navigate(`/plugins/${p.name}`)}
                    className="px-3 py-1 text-sm rounded bg-neutral-700 hover:bg-neutral-600 text-neutral-200">{t('열기')}</button>
                  {m?.ui && (
                    <button onClick={() => openPluginUi(p.name, m.ui)}
                      className="px-3 py-1 text-sm rounded bg-neutral-700 hover:bg-neutral-600 text-neutral-200">{t('UI 보기')}</button>
                  )}
                </div>
              </div>
            )
          })}
        </div>
      )}
    </div>
  )
}
