import { useEffect, useState } from 'react'
import { api } from '../services/api'
import type { Model } from '../types/models'
import DiskUsageBar from '../components/DiskUsageBar'
import HubBrowser from '../components/HubBrowser'

function formatBytes(bytes: number): string {
  if (bytes === 0) return '0 B'
  const k = 1024
  const sizes = ['B', 'KB', 'MB', 'GB', 'TB']
  const i = Math.floor(Math.log(bytes) / Math.log(k))
  return `${(bytes / Math.pow(k, i)).toFixed(1)} ${sizes[i]}`
}

type SortKey = 'name' | 'size' | 'date' | 'policy'

export default function ModelsPage({ embedded = false }: { embedded?: boolean }) {
  const [tab, setTab] = useState<'local' | 'hub'>('local')
  const [models, setModels] = useState<Model[]>([])
  const [loading, setLoading] = useState(true)
  const [selectedId, setSelectedId] = useState<string | null>(null)
  const [search, setSearch] = useState('')
  const [sortKey, setSortKey] = useState<SortKey>('date')
  const [sortAsc, setSortAsc] = useState(false)

  const fetchModels = () => {
    setLoading(true)
    api
      .get<Model[]>('/models')
      .then(setModels)
      .catch(() => {})
      .finally(() => setLoading(false))
  }

  useEffect(() => {
    fetchModels()
  }, [])

  const handleDelete = async (id: string) => {
    if (!confirm(`"${id}" 모델을 삭제하시겠습니까?`)) return
    await api.delete(`/models/${id}`)
    fetchModels()
    if (selectedId === id) setSelectedId(null)
  }

  const handleStartInference = async (model: Model) => {
    try {
      await api.post('/models/inference/start', {
        checkpoint_path: model.path,
      })
    } catch (e) {
      alert('추론 시작 실패')
    }
  }

  const selected = models.find((m) => m.id === selectedId)

  const filteredModels = models
    .filter((m) => !search || m.id.toLowerCase().includes(search.toLowerCase()) || m.policy_type.toLowerCase().includes(search.toLowerCase()))
    .sort((a, b) => {
      let cmp = 0
      if (sortKey === 'name') cmp = a.id.localeCompare(b.id)
      else if (sortKey === 'size') cmp = a.size_bytes - b.size_bytes
      else if (sortKey === 'date') cmp = new Date(a.modified).getTime() - new Date(b.modified).getTime()
      else if (sortKey === 'policy') cmp = a.policy_type.localeCompare(b.policy_type)
      return sortAsc ? cmp : -cmp
    })

  const handleSort = (key: SortKey) => {
    if (sortKey === key) setSortAsc(!sortAsc)
    else { setSortKey(key); setSortAsc(false) }
  }

  return (
    <div className="space-y-6">
      {!embedded && (
        <div className="flex items-center justify-between">
          <h1 className="text-2xl font-bold">모델</h1>
          <div className="flex gap-1">
            <button onClick={() => setTab('local')}
              className={`px-3 py-1.5 rounded text-sm ${tab === 'local' ? 'bg-neutral-700 text-white' : 'text-neutral-400 hover:text-white'}`}>
              로컬
            </button>
            <button onClick={() => setTab('hub')}
              className={`px-3 py-1.5 rounded text-sm ${tab === 'hub' ? 'bg-neutral-700 text-white' : 'text-neutral-400 hover:text-white'}`}>
              Hub
            </button>
            <button onClick={fetchModels} disabled={loading}
              className="px-3 py-1.5 rounded text-sm text-neutral-400 hover:text-white hover:bg-neutral-700 disabled:opacity-50">
              {loading ? '스캔 중...' : '새로고침'}
            </button>
          </div>
        </div>
      )}
      {embedded && (
        <div className="flex gap-1">
          <button onClick={() => setTab('local')}
            className={`px-3 py-1.5 rounded text-sm ${tab === 'local' ? 'bg-neutral-700 text-white' : 'text-neutral-400 hover:text-white'}`}>
            로컬
          </button>
          <button onClick={() => setTab('hub')}
            className={`px-3 py-1.5 rounded text-sm ${tab === 'hub' ? 'bg-neutral-700 text-white' : 'text-neutral-400 hover:text-white'}`}>
            Hub
          </button>
          <button onClick={fetchModels} disabled={loading}
            className="px-3 py-1.5 rounded text-sm text-neutral-400 hover:text-white hover:bg-neutral-700 disabled:opacity-50">
            {loading ? '스캔 중...' : '새로고침'}
          </button>
        </div>
      )}

      <DiskUsageBar />

      {tab === 'hub' ? <HubBrowser type="models" /> : null}

      {tab === 'local' && loading ? (
        <p className="text-neutral-400">스캔 중...</p>
      ) : tab === 'local' && models.length === 0 ? (
        <p className="text-neutral-400">
          로컬 모델이 없습니다. Hub에서 다운로드하거나 학습을 실행하세요.
        </p>
      ) : tab === 'local' ? (
        <div className="grid grid-cols-1 lg:grid-cols-3 gap-4">
          {/* 모델 목록 */}
          <div className="lg:col-span-1 space-y-2">
            <input type="text" value={search} onChange={(e) => setSearch(e.target.value)}
              placeholder="검색 (이름, 정책)..."
              className="w-full px-3 py-1.5 rounded bg-neutral-900 border border-neutral-700 text-sm text-neutral-100 placeholder:text-neutral-500" />
            <div className="flex gap-1 text-[10px]">
              {([['name', '이름'], ['policy', '정책'], ['size', '크기'], ['date', '날짜']] as [SortKey, string][]).map(([k, l]) => (
                <button key={k} onClick={() => handleSort(k)}
                  className={`px-2 py-0.5 rounded ${sortKey === k ? 'bg-blue-600 text-white' : 'bg-neutral-700 text-neutral-400'}`}>
                  {l}{sortKey === k ? (sortAsc ? ' ↑' : ' ↓') : ''}
                </button>
              ))}
              <span className="ml-auto text-neutral-500">{filteredModels.length}/{models.length}</span>
            </div>
            {filteredModels.map((m) => (
              <div
                key={m.id}
                onClick={() => setSelectedId(m.id)}
                className={`rounded-lg border p-4 cursor-pointer transition-colors ${
                  selectedId === m.id
                    ? 'border-blue-500 bg-blue-500/10'
                    : 'border-neutral-700 bg-neutral-800 hover:border-neutral-600'
                }`}
              >
                <div className="flex justify-between items-start">
                  <div className="min-w-0">
                    <p className="font-medium truncate">{m.id}</p>
                    <p className="text-xs text-neutral-400 mt-1">
                      {m.policy_type} · {formatBytes(m.size_bytes)}
                    </p>
                  </div>
                </div>
                <div className="flex gap-2 mt-3">
                  <button
                    onClick={(e) => {
                      e.stopPropagation()
                      handleStartInference(m)
                    }}
                    className="px-3 py-1 text-xs rounded bg-blue-600 hover:bg-blue-500 text-white"
                  >
                    추론 시작
                  </button>
                  <button
                    onClick={(e) => {
                      e.stopPropagation()
                      handleDelete(m.id)
                    }}
                    className="px-3 py-1 text-xs rounded bg-neutral-700 hover:bg-red-600 text-neutral-300 hover:text-white"
                  >
                    삭제
                  </button>
                </div>
              </div>
            ))}
          </div>

          {/* 상세 패널 */}
          <div className="lg:col-span-2">
            {selected ? (
              <div className="rounded-lg border border-neutral-700 bg-neutral-800 p-6 space-y-5">
                <h2 className="text-lg font-semibold">{selected.id}</h2>

                {/* 기본 정보 */}
                <div className="grid grid-cols-2 sm:grid-cols-4 gap-3 text-sm">
                  <div>
                    <span className="text-neutral-400 text-xs">Policy</span>
                    <p className="font-medium">{selected.policy_type}</p>
                  </div>
                  <div>
                    <span className="text-neutral-400 text-xs">크기</span>
                    <p>{formatBytes(selected.size_bytes)}</p>
                  </div>
                  <div>
                    <span className="text-neutral-400 text-xs">수정일</span>
                    <p>{new Date(selected.modified).toLocaleString()}</p>
                  </div>
                  <div>
                    <span className="text-neutral-400 text-xs">Repo</span>
                    <p className="truncate text-xs">{(selected.config.repo_id as string) || selected.id}</p>
                  </div>
                </div>

                {/* 입출력 요구사항 */}
                {selected.requirements && (
                  <div className="space-y-2">
                    <h3 className="text-sm font-semibold text-neutral-300">입출력</h3>
                    <div className="grid grid-cols-2 sm:grid-cols-3 gap-3 text-sm">
                      <div>
                        <span className="text-neutral-400 text-xs">관절 (state)</span>
                        <p>{selected.requirements.state_dim}개</p>
                      </div>
                      <div>
                        <span className="text-neutral-400 text-xs">액션</span>
                        <p>{selected.requirements.action_dim}차원</p>
                      </div>
                      <div>
                        <span className="text-neutral-400 text-xs">카메라</span>
                        <p>{selected.requirements.required_cameras.length}대</p>
                      </div>
                    </div>
                    {selected.requirements.required_cameras.length > 0 && (
                      <div className="rounded border border-neutral-700 overflow-hidden">
                        <table className="w-full text-xs">
                          <thead className="bg-neutral-900">
                            <tr>
                              <th className="p-2 text-left text-neutral-400">이름</th>
                              <th className="p-2 text-left text-neutral-400">해상도</th>
                              <th className="p-2 text-left text-neutral-400">채널</th>
                            </tr>
                          </thead>
                          <tbody>
                            {selected.requirements.required_cameras.map((cam) => (
                              <tr key={cam.name} className="border-t border-neutral-700">
                                <td className="p-2 font-medium">{cam.name}</td>
                                <td className="p-2 text-neutral-300">{cam.width}x{cam.height}</td>
                                <td className="p-2 text-neutral-400">{cam.channels}</td>
                              </tr>
                            ))}
                          </tbody>
                        </table>
                      </div>
                    )}
                  </div>
                )}

                {/* 아키텍처 */}
                <div className="space-y-2">
                  <h3 className="text-sm font-semibold text-neutral-300">아키텍처</h3>
                  <div className="grid grid-cols-2 sm:grid-cols-3 gap-x-4 gap-y-1 text-xs">
                    {selected.config.vlm_model_name != null && (
                      <div className="col-span-2 sm:col-span-3">
                        <span className="text-neutral-400">VLM</span>
                        <span className="ml-2 text-neutral-100">{String(selected.config.vlm_model_name)}</span>
                      </div>
                    )}
                    {[
                      ['chunk_size', '청크 크기'],
                      ['n_action_steps', '액션 스텝'],
                      ['n_obs_steps', '관측 스텝'],
                      ['num_vlm_layers', 'VLM 레이어'],
                      ['num_expert_layers', '전문가 레이어'],
                      ['num_steps', '디노이징 스텝'],
                      ['attention_mode', '어텐션 모드'],
                      ['freeze_vision_encoder', '비전 동결'],
                      ['use_cache', '캐시 사용'],
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
                  <summary className="text-sm text-neutral-400 cursor-pointer">학습 설정</summary>
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
                    <summary className="text-sm text-neutral-400 cursor-pointer">정규화</summary>
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
                  <summary className="text-sm text-neutral-400 cursor-pointer">Config JSON (전체)</summary>
                  <pre className="mt-2 text-xs bg-neutral-900 p-3 rounded overflow-auto max-h-64">
                    {JSON.stringify(selected.config, null, 2)}
                  </pre>
                </details>
              </div>
            ) : (
              <div className="rounded-lg border border-neutral-700 bg-neutral-800 p-6 text-neutral-400 text-center">
                모델을 선택하면 상세 정보가 표시됩니다
              </div>
            )}
          </div>
        </div>
      ) : null}
    </div>
  )
}
