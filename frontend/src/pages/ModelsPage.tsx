import { useCallback, useEffect, useState } from 'react'
import NotesEditor from '../components/NotesEditor'
import { useSystemMessage } from '../components/SystemMessages'
import { api } from '../services/api'
import { useWebSocket, type WsMessage } from '../hooks/useWebSocket'
import LogViewer from '../components/LogViewer'
import RepoIdInput, { repoIdError } from '../components/RepoIdInput'
import type { Model, WeightRun } from '../types/models'
import { sizeText } from '../types/camera'
import DiskUsageBar from '../components/DiskUsageBar'
import HubBrowser from '../components/HubBrowser'
import WeightTable, { CheckpointChip, RunSummary } from '../components/WeightTable'
import { useT, fmt } from '../i18n'

function formatBytes(bytes: number): string {
  if (bytes === 0) return '0 B'
  const k = 1024
  const sizes = ['B', 'KB', 'MB', 'GB', 'TB']
  const i = Math.floor(Math.log(bytes) / Math.log(k))
  return `${(bytes / Math.pow(k, i)).toFixed(1)} ${sizes[i]}`
}

export default function ModelsPage({ embedded = false, tab: tabProp, refreshKey }: { embedded?: boolean; tab?: 'local' | 'hub'; refreshKey?: number }) {
  const t = useT()
  // ⚠ `window.alert` 를 쓰지 않는다 — 이벤트 루프를 막아 E-stop heartbeat 가
  //   끊기고, 2초 타임아웃에 추론이 강제 종료된다 (confirm 으로 실제로 겪었다).
  const { notify, confirm: askConfirm } = useSystemMessage()
  const notifyError = (text: string) =>
    notify({ level: 'error', text, source: t('추론') })
  const [ownTab, setTab] = useState<'local' | 'hub'>('local')
  // ⚠ **묶여 있을 때는 부모가 정한다.** `로컬|Hub` 는 모델·데이터셋 두 탭이
  //   공유하는 선택이라, 각 페이지가 따로 들고 있으면 탭을 옮길 때마다 되돌아간다.
  const tab = tabProp ?? ownTab

  // 부모(저장소 화면)의 "새로고침" 을 받는다
  useEffect(() => { if (refreshKey !== undefined) fetchModels() }, [refreshKey])
  // 표는 학습 단위(`/models/runs`), 펼친 체크포인트의 상세는 모델 단위(`/models`)다
  const [models, setModels] = useState<Model[]>([])
  const [runs, setRuns] = useState<WeightRun[]>([])
  const [loading, setLoading] = useState(true)
  // ⚠ 못 읽은 것을 "모델이 없습니다" 로 보이면 안 된다 — 지운 줄 안다
  const [loadError, setLoadError] = useState('')
  // 상세 창 — 어느 학습의 어느 체크포인트
  const [open, setOpen] = useState<{ key: string; ckpt: string } | null>(null)
  // Hub 업로드 — 올릴 저장소를 묻는 창, 그다음 로그 창 (데이터셋 업로드와 같은 유닛·같은 WS 메시지)
  const [uploadAsk, setUploadAsk] = useState<{ id: string; label: string; repoId: string; private: boolean } | null>(null)
  const [uploadRepo, setUploadRepo] = useState<string | null>(null)
  const [uploadLogs, setUploadLogs] = useState<string[]>([])
  const [uploadState, setUploadState] = useState('idle')
  const uploading = uploadState === 'running' || uploadState === 'starting'

  useWebSocket('/ws', {
    onMessage: useCallback((msg: WsMessage) => {
      if (msg.type === 'upload_log') setUploadLogs((prev) => [...prev.slice(-200), msg.data as string])
      else if (msg.type === 'upload_state') setUploadState(msg.data as string)
    }, []),
  })

  /** 올릴 저장소를 묻는다. 이름은 "정책-데이터셋-스텝" 으로 채워 두고 네임스페이스는 내 계정 —
   *  모델 id 는 학습 경로(`2026-10-27/…/020000`)라 그대로는 저장소 이름이 못 된다. */
  const askUpload = async (id: string, row: WeightRun) => {
    const c = row.checkpoints.find((x) => x.ids.includes(id)) ?? row.checkpoints[0]
    const slug = [row.policy_type, row.dataset?.split('/').pop(),
      c?.step != null ? (c.step % 1000 === 0 ? `${c.step / 1000}k` : String(c.step)) : c?.checkpoint]
      .filter(Boolean).join('-').replace(/[^\w.-]+/g, '-').replace(/-+/g, '-').slice(0, 96)
    const who = await api.get<{ logged_in: boolean; username: string }>('/hub/whoami').catch(() => null)
    setUploadAsk({ id, label: `${row.name || row.key} · ${c?.checkpoint ?? id}`,
                   repoId: who?.logged_in && who.username ? `${who.username}/${slug}` : slug, private: false })
  }

  const startUpload = async () => {
    if (!uploadAsk) return
    try {
      await api.post(`/models/${uploadAsk.id}/upload`, { repo_id: uploadAsk.repoId.trim(), private: uploadAsk.private })
      setUploadRepo(uploadAsk.repoId.trim()); setUploadLogs([]); setUploadState('starting'); setUploadAsk(null)
    } catch (e) {
      notify({ level: 'error', source: t('모델'), text: e instanceof Error ? e.message : t('업로드 실패') })
    }
  }

  const fetchModels = () => {
    setLoading(true)
    Promise.all([api.get<Model[]>('/models'), api.get<WeightRun[]>('/models/runs')])
      .then(([m, r]) => { setModels(m); setRuns(r); setLoadError('') })
      .catch((e) => setLoadError(e instanceof Error ? e.message : t('목록을 읽지 못했습니다')))
      .finally(() => setLoading(false))
  }

  useEffect(() => {
    fetchModels()
  }, [])

  const handleDelete = async (id: string) => {
    const model = models.find((m) => m.id === id)
    const isLocal = model?.path && !model.path.includes('.cache/huggingface/hub/models--')
    const msg = isLocal
      ? t('"{id}" 모델 폴더를 삭제하시겠습니까?\n경로: {path}', { id, path: model.path })
      : t('"{id}" 모델을 삭제하시겠습니까?', { id })
    if (!await askConfirm(msg)) return
    await api.delete(`/models/${id}`)
    fetchModels()
  }

  const handleStartInference = async (model: Model) => {
    try {
      await api.post('/models/inference/start', {
        checkpoint_path: model.path,
      })
    } catch (e) {
      notifyError(t('추론 시작 실패'))
    }
  }

  return (
    <div className="space-y-6">
      {!embedded && (
        <div className="flex items-center justify-between">
          <h1 className="text-2xl font-bold">{t('모델')}</h1>
          <div className="flex gap-1">
            <button onClick={() => setTab('local')}
              className={`px-3 py-1.5 rounded text-sm ${tab === 'local' ? 'bg-neutral-700 text-white' : 'text-neutral-400 hover:text-white'}`}>
              {t('로컬')}
            </button>
            <button onClick={() => setTab('hub')}
              className={`px-3 py-1.5 rounded text-sm ${tab === 'hub' ? 'bg-neutral-700 text-white' : 'text-neutral-400 hover:text-white'}`}>
              Hub
            </button>
            <button onClick={fetchModels} disabled={loading}
              className="px-3 py-1.5 rounded text-sm text-neutral-400 hover:text-white hover:bg-neutral-700 disabled:opacity-50">
              {loading ? t('스캔 중...') : t('새로고침')}
            </button>
          </div>
        </div>
      )}

      {!embedded && <DiskUsageBar />}

      {tab === 'hub' ? <HubBrowser type="models" /> : null}

      {tab === 'local' && loading && !runs.length ? (
        <p className="text-neutral-400">{t('스캔 중...')}</p>
      ) : tab === 'local' && loadError && !runs.length ? (
        <p className="text-red-300">{t('목록을 읽지 못했습니다')} — {loadError}</p>
      ) : tab === 'local' && runs.length === 0 ? (
        <p className="text-neutral-400">
          {t('로컬 모델이 없습니다. Hub에서 다운로드하거나 학습을 실행하세요.')}
        </p>
      ) : tab === 'local' ? (
        // ⚠ 학습이 표의 단위다. 예전에는 왼쪽 좁은 목록에 학습을 접어 두었고, 그 전에는
        //   체크포인트 72개가 평면으로 깔려 어느 학습의 몇 번째인지 읽을 수가 없었다.
        //   행을 누르면 상세가 창으로 뜬다 — 칩을 누르면 그 체크포인트로 연다 (데이터셋 화면과 같다).
        <WeightTable rows={runs} onRenamed={fetchModels} value={open?.ckpt}
          onOpen={(row, ckpt) => setOpen({ key: row.key, ckpt })}
          // 학습한 것만 — 허브에서 받은 모델을 다시 올릴 일은 없다. 행이면 마지막 체크포인트
          actions={(row) => row.kind === 'run' && row.checkpoints[0] ? (
            <button type="button" onClick={() => void askUpload(row.checkpoints[0].id, row)}
              className="rounded bg-neutral-700 px-2 py-0.5 text-xs text-neutral-300 hover:bg-blue-600 hover:text-white">
              {t('Hub 업로드')}
            </button>
          ) : null} />
      ) : null}

      {tab === 'local' && open && (() => {
        // ⚠ 다시 읽은 목록에서 찾는다 — 체크포인트를 지우면 그 칸이, 마지막 것을 지우면 학습이 사라진다
        const row = runs.find((r) => r.key === open.key)
        if (!row || !row.checkpoints.length) return null
        const ckpt = row.checkpoints.find((c) => c.ids.includes(open.ckpt)) ?? row.checkpoints[0]
        const m = models.find((x) => x.id === ckpt.id)
        return (
          // ⚠ Esc 로 닫지 않는다(전역 E-stop) — ✕ 와 바깥 클릭으로 닫는다. 확인 창(z-[60])은 이 위에 뜬다
          <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4" onClick={() => setOpen(null)}>
            <div className="flex max-h-[90vh] w-full max-w-5xl flex-col rounded-lg border border-neutral-700 bg-neutral-900"
              onClick={(e) => e.stopPropagation()}>
              <div className="flex items-start gap-3 border-b border-neutral-800 px-5 py-3">
                <div className="min-w-0">
                  <h2 className="truncate text-lg font-semibold">{row.name || row.key}</h2>
                  <p className="truncate text-xs text-neutral-500">
                    {[row.name && row.key, row.dataset, row.policy_type].filter(Boolean).join(' · ')}
                  </p>
                </div>
                <button type="button" onClick={() => setOpen(null)} title={t('닫기')}
                  className="ml-auto rounded px-2 py-0.5 text-neutral-400 hover:bg-neutral-800 hover:text-white">✕</button>
              </div>
              <div className="min-h-0 space-y-4 overflow-y-auto p-5 text-xs">
                {/* 학습 단위 이름·설명 — 학습 폼의 제목·설명과 같은 파일 */}
                {row.kind === 'run' && (
                  <div className="space-y-1">
                    <p className="text-[11px] text-neutral-400">{t('학습 이름·설명 — 모든 체크포인트가 물려받습니다')}</p>
                    {/* 보이는 이름이 체크포인트에서 빌려 온 것이면 여기가 비어 보인다 — 왜 비었는지 말한다 */}
                    {row.name && !(row.notes as { name?: string }).name && (
                      <p className="text-[11px] text-amber-300/90">
                        {t('지금 보이는 이름 "{name}" 은 체크포인트에 붙은 것입니다 — 여기에 적으면 학습 이름이 됩니다', { name: row.name })}
                      </p>
                    )}
                    <NotesEditor key={row.key} endpoint={`/models/runs/${row.run}/notes`} source={t('모델')} onSaved={fetchModels} />
                  </div>
                )}
                <RunSummary row={row} rows={runs} />
                {/* 체크포인트 — 눌러서 아래 상세를 바꾼다 */}
                {row.kind === 'run' && (
                  <div className="flex flex-wrap items-center gap-1">
                    <span className="mr-1 text-neutral-500">{t('체크포인트')}</span>
                    {row.checkpoints.map((c) => (
                      <CheckpointChip key={c.id} c={c} row={row} on={c.id === ckpt.id}
                        onClick={() => setOpen({ key: row.key, ckpt: c.id })} />
                    ))}
                  </div>
                )}
                {m && (
                  <CheckpointDetail model={m} ownNotes={row.kind === 'run'}
                    onUpload={row.kind === 'run' ? () => void askUpload(m.id, row) : undefined}
                    onStartInference={() => void handleStartInference(m)}
                    onDelete={() => void handleDelete(m.id)} onSaved={fetchModels} />
                )}
              </div>
            </div>
          </div>
        )
      })()}

      {/* 올릴 저장소 묻기 — ⚠ Esc 로 닫지 않는다(전역 E-stop) */}
      {uploadAsk && (() => {
        const err = repoIdError(uploadAsk.repoId)
        const ok = !!uploadAsk.repoId.trim() && !err
        return (
          <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4" onClick={() => setUploadAsk(null)}>
            <div className="w-full max-w-lg space-y-3 rounded-lg border border-neutral-700 bg-neutral-900 p-4 text-sm"
              onClick={(e) => e.stopPropagation()}>
              <h3 className="font-semibold text-neutral-100">{t('Hub 에 올리기')}</h3>
              <p className="text-xs text-neutral-400">{uploadAsk.label}</p>
              <div className="space-y-1">
                <label className="text-xs text-neutral-400">{t('올릴 모델 저장소')}</label>
                <RepoIdInput value={uploadAsk.repoId} onChange={(v) => setUploadAsk({ ...uploadAsk, repoId: v })} />
                {err && <p className="text-xs text-amber-400">{err}</p>}
              </div>
              <label className="flex items-center gap-2 text-xs text-neutral-300">
                <input type="checkbox" checked={uploadAsk.private}
                  onChange={(e) => setUploadAsk({ ...uploadAsk, private: e.target.checked })} />
                {t('비공개 저장소로 만들기')}
              </label>
              <p className="text-[11px] text-neutral-500">
                {t('체크포인트 폴더(config·가중치·전처리)를 올립니다. 저장소가 없으면 새로 만들고, 있으면 새 커밋으로 올라갑니다.')}
              </p>
              <div className="flex justify-end gap-2">
                <button type="button" onClick={() => setUploadAsk(null)}
                  className="rounded bg-neutral-700 px-3 py-1 text-xs text-neutral-300 hover:bg-neutral-600">{t('취소')}</button>
                <button type="button" disabled={!ok} onClick={() => void startUpload()}
                  className="rounded bg-blue-600 px-3 py-1 text-xs text-white hover:bg-blue-500 disabled:opacity-50">{t('올리기')}</button>
              </div>
            </div>
          </div>
        )
      })()}

      {/* 업로드 진행 — 데이터셋 업로드와 같은 유닛이라 중지도 같다. 도는 동안은 닫지 않는다 */}
      {uploadRepo && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60" onClick={() => { if (!uploading) setUploadRepo(null) }}>
          <div className="bg-neutral-800 border border-neutral-600 rounded-lg w-[600px] max-h-[80vh] flex flex-col" onClick={(e) => e.stopPropagation()}>
            <div className="flex items-center justify-between p-4 border-b border-neutral-700">
              <h3 className="text-sm font-semibold">{t('Hub 업로드: {uploadId}', { uploadId: uploadRepo })}</h3>
              <span className={`px-2 py-0.5 rounded text-[10px] font-medium ${
                uploading ? 'bg-blue-500/20 text-blue-400' :
                uploadState === 'error' ? 'bg-red-500/20 text-red-400' :
                uploadState === 'idle' ? 'bg-green-500/20 text-green-400' : 'bg-neutral-700 text-neutral-400'
              }`}>{uploadState}</span>
            </div>
            <div className="flex-1 overflow-hidden p-2">
              <LogViewer logs={uploadLogs} height="min-h-[280px] max-h-[50vh]" placeholder={t('시작 대기...')} searchable />
            </div>
            <div className="p-3 border-t border-neutral-700 flex justify-end gap-2">
              {uploading ? (
                <button onClick={() => api.post('/datasets/upload-stop')} className="px-3 py-1 text-xs rounded bg-red-600 hover:bg-red-500 text-white">{t('중지')}</button>
              ) : (
                <button onClick={() => setUploadRepo(null)} className="px-3 py-1 text-xs rounded bg-neutral-700 hover:bg-neutral-600 text-neutral-300">{t('닫기')}</button>
              )}
            </div>
          </div>
        </div>
      )}
    </div>
  )
}

/** 체크포인트 하나의 상세 — 입출력·아키텍처·학습 설정·Config. */
function CheckpointDetail({ model: selected, ownNotes, onUpload, onStartInference, onDelete, onSaved }: {
  model: Model
  /** 이 체크포인트를 Hub 에 올린다 (학습 산출물만) */
  onUpload?: () => void
  /** 학습 산출물이면 이 편집기는 "이 체크포인트만" 의 이름이다 — 비우면 학습 이름을 따른다 */
  ownNotes: boolean
  onStartInference: () => void
  onDelete: () => void
  onSaved: () => void
}) {
  const t = useT()
  return (
    <div className="rounded-lg border border-neutral-700 bg-neutral-800 p-4 space-y-5">
      <div className="flex flex-wrap items-center gap-2">
        <h2 className="text-sm font-semibold">{selected.id}</h2>
        <div className="ml-auto flex gap-2">
          {onUpload && (
            <button onClick={onUpload}
              className="px-3 py-1 text-xs rounded bg-neutral-700 hover:bg-blue-600 text-neutral-300 hover:text-white">
              {t('Hub 업로드')}
            </button>
          )}
          <button onClick={onStartInference}
            className="px-3 py-1 text-xs rounded bg-blue-600 hover:bg-blue-500 text-white">
            {t('추론 시작')}
          </button>
          <button onClick={onDelete}
            className="px-3 py-1 text-xs rounded bg-neutral-700 hover:bg-red-600 text-neutral-300 hover:text-white">
            {t('삭제')}
          </button>
        </div>
      </div>

      {/* 이름·설명 사이드카 — 체크포인트 형식에 이 자리가 없다 */}
      <div className="space-y-1">
        {ownNotes && <p className="text-[11px] text-neutral-400">{t('이 체크포인트만의 이름·설명 — 비워 두면 학습 이름을 따릅니다')}</p>}
        <NotesEditor key={selected.id} endpoint={`/models/${selected.id}/notes`} source={t('모델')} onSaved={onSaved} />
      </div>

      {/* 기본 정보 */}
      <div className="grid grid-cols-2 sm:grid-cols-4 gap-3 text-sm">
        <div>
          <span className="text-neutral-400 text-xs">Policy</span>
          <p className="font-medium">
            {selected.policy_type}
            {selected.action_space === 'delta_v1' && <span className="ml-1 text-emerald-300 text-xs">Δ {t('델타')}</span>}
            {selected.action_space === 'unknown' && <span className="ml-1 text-red-400 text-xs">Δ? {t('확인 불가')}</span>}
          </p>
        </div>
        <div>
          <span className="text-neutral-400 text-xs">{t('크기')}</span>
          <p>{formatBytes(selected.size_bytes)}</p>
        </div>
        <div>
          <span className="text-neutral-400 text-xs">{t('수정일')}</span>
          <p>{fmt.dateTime(selected.modified)}</p>
        </div>
        <div>
          <span className="text-neutral-400 text-xs">Repo</span>
          <p className="truncate text-xs">{(selected.config.repo_id as string) || selected.id}</p>
        </div>
      </div>

      {/* 입출력 요구사항 */}
      {selected.requirements && (
        <div className="space-y-2">
          <h3 className="text-sm font-semibold text-neutral-300">{t('입출력')}</h3>
          <div className="grid grid-cols-2 sm:grid-cols-3 gap-3 text-sm">
            <div>
              <span className="text-neutral-400 text-xs">{t('관절 (state)')}</span>
              <p>{t('{state_dim}개', { state_dim: selected.requirements.state_dim })}</p>
            </div>
            <div>
              <span className="text-neutral-400 text-xs">{t('액션')}</span>
              <p>{t('{action_dim}차원', { action_dim: selected.requirements.action_dim })}</p>
            </div>
            <div>
              <span className="text-neutral-400 text-xs">{t('카메라')}</span>
              <p>{t('{length}대', { length: selected.requirements.required_cameras.length })}</p>
            </div>
          </div>
          {selected.requirements.required_cameras.length > 0 && (
            <div className="rounded border border-neutral-700 overflow-hidden">
              <table className="w-full text-xs">
                <thead className="bg-neutral-900">
                  <tr>
                    <th className="p-2 text-left text-neutral-400">{t('이름')}</th>
                    <th className="p-2 text-left text-neutral-400"
                      title={t('학습 데이터셋을 녹화한 해상도 — 추론 카메라가 이것과 같아야 합니다')}>{t('학습 해상도')}</th>
                    <th className="p-2 text-left text-neutral-400">{t('기종')}</th>
                    <th className="p-2 text-left text-neutral-400"
                      title={t('모델 config 의 입력 크기 — 정책이 안에서 줄이는 크기입니다. 베이스 모델에서 시작하면 베이스의 값이 남습니다')}>{t('모델 입력')}</th>
                    <th className="p-2 text-left text-neutral-400">{t('채널')}</th>
                  </tr>
                </thead>
                <tbody>
                  {selected.requirements.required_cameras.map((cam) => {
                    const tc = selected.train_cameras?.cameras[cam.name]
                    return (
                      <tr key={cam.name} className="border-t border-neutral-700">
                        <td className="p-2 font-medium">{cam.name}</td>
                        <td className="p-2 font-mono text-neutral-200">
                          {tc ? sizeText(tc.width, tc.height, tc.fps) : <span className="text-neutral-500">{t('모름')}</span>}
                        </td>
                        <td className="p-2 text-neutral-300">{tc?.model ?? <span className="text-neutral-500">—</span>}</td>
                        <td className="p-2 font-mono text-neutral-400">{sizeText(cam.width, cam.height)}</td>
                        <td className="p-2 text-neutral-400">{cam.channels}</td>
                      </tr>
                    )
                  })}
                </tbody>
              </table>
            </div>
          )}
        </div>
      )}

      {/* 아키텍처 */}
      <div className="space-y-2">
        <h3 className="text-sm font-semibold text-neutral-300">{t('아키텍처')}</h3>
        <div className="grid grid-cols-2 sm:grid-cols-3 gap-x-4 gap-y-1 text-xs">
          {selected.config.vlm_model_name != null && (
            <div className="col-span-2 sm:col-span-3">
              <span className="text-neutral-400">VLM</span>
              <span className="ml-2 text-neutral-100">{String(selected.config.vlm_model_name)}</span>
            </div>
          )}
          {[
            ['chunk_size', t('청크 크기')],
            ['n_action_steps', t('액션 스텝')],
            ['n_obs_steps', t('관측 스텝')],
            ['num_vlm_layers', t('VLM 레이어')],
            ['num_expert_layers', t('전문가 레이어')],
            ['num_steps', t('디노이징 스텝')],
            ['attention_mode', t('어텐션 모드')],
            ['freeze_vision_encoder', t('비전 동결')],
            ['use_cache', t('캐시 사용')],
          ].map(([key, label]) => {
            const val = selected.config[key]
            if (val == null) return null
            return (
              <div key={key}>
                <span className="text-neutral-400">{label}</span>
                <span className="ml-2 text-neutral-100">{String(val)}</span>
              </div>
            )
          })}
        </div>
      </div>

      {/* 학습 설정 */}
      <details>
        <summary className="text-sm text-neutral-400 cursor-pointer">{t('학습 설정')}</summary>
        <div className="mt-2 grid grid-cols-2 sm:grid-cols-3 gap-x-4 gap-y-1 text-xs">
          {[
            ['optimizer_lr', 'Learning Rate'],
            ['optimizer_weight_decay', 'Weight Decay'],
            ['optimizer_grad_clip_norm', 'Grad Clip'],
            ['scheduler_warmup_steps', 'Warmup Steps'],
            ['scheduler_decay_steps', 'Decay Steps'],
            ['scheduler_decay_lr', 'Decay LR'],
            ['use_amp', 'AMP'],
            ['device', 'Device'],
          ].map(([key, label]) => {
            const val = selected.config[key]
            if (val == null) return null
            return (
              <div key={key}>
                <span className="text-neutral-400">{label}</span>
                <span className="ml-2 text-neutral-100">{String(val)}</span>
              </div>
            )
          })}
        </div>
      </details>

      {/* 정규화 매핑 */}
      {selected.config.normalization_mapping != null && (
        <details>
          <summary className="text-sm text-neutral-400 cursor-pointer">{t('정규화')}</summary>
          <div className="mt-2 grid grid-cols-3 gap-x-4 gap-y-1 text-xs">
            {Object.entries(selected.config.normalization_mapping as Record<string, string>).map(([k, v]) => (
              <div key={k}>
                <span className="text-neutral-400">{k}</span>
                <span className="ml-2 text-neutral-100">{v}</span>
              </div>
            ))}
          </div>
        </details>
      )}

      {/* Config JSON (raw) */}
      <details>
        <summary className="text-sm text-neutral-400 cursor-pointer">{t('Config JSON (전체)')}</summary>
        <pre className="mt-2 text-xs bg-neutral-900 p-3 rounded overflow-auto max-h-64">
          {JSON.stringify(selected.config, null, 2)}
        </pre>
      </details>
    </div>
  )
}
