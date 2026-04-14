import { useState, useEffect } from 'react'
import { api } from '../services/api'

type HubItem = {
  repo_id: string
  author: string
  downloads: number
  last_modified: string | null
  tags: string[]
  base_model?: string | null
  datasets?: string[]
  policy_type?: string | null
  is_base?: boolean
}

type Props = {
  type: 'models' | 'datasets'
}

export default function HubBrowser({ type }: Props) {
  const [items, setItems] = useState<HubItem[]>([])
  const [query, setQuery] = useState('')
  const [author, setAuthor] = useState('')
  const [loading, setLoading] = useState(false)
  const [downloading, setDownloading] = useState<Set<string>>(new Set())
  const search = (authorOverride?: string) => {
    const a = authorOverride ?? author
    setLoading(true)
    api
      .get<HubItem[]>(
        `/hub/${type}?q=${encodeURIComponent(query)}&author=${encodeURIComponent(a)}`,
      )
      .then(setItems)
      .catch(() => {})
      .finally(() => setLoading(false))
  }

  useEffect(() => {
    // HF 로그인 계정을 기본 author로 설정
    api.get<{ logged_in: boolean; username: string }>('/hub/whoami')
      .then((info) => {
        if (info.logged_in && info.username) {
          setAuthor(info.username)
          search(info.username)
        } else {
          setAuthor('lerobot')
          search('lerobot')
        }
      })
      .catch(() => {
        setAuthor('lerobot')
        search('lerobot')
      })
  }, [type])

  const handleDownload = async (repoId: string) => {
    setDownloading((prev) => new Set(prev).add(repoId))
    try {
      await api.post('/hub/download', {
        repo_id: repoId,
        repo_type: type === 'models' ? 'model' : 'dataset',
      })
    } catch {
      // ignore
    }
  }

  return (
    <div className="space-y-4">
      {/* 검색 */}
      <div className="flex gap-2">
        <input
          type="text"
          value={author}
          onChange={(e) => setAuthor(e.target.value)}
          placeholder="Author"
          className="w-32 px-3 py-1.5 rounded bg-neutral-800 border border-neutral-700 text-sm text-neutral-100 focus:outline-none focus:border-blue-500"
        />
        <input
          type="text"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          onKeyDown={(e) => e.key === 'Enter' && search()}
          placeholder="검색..."
          className="flex-1 px-3 py-1.5 rounded bg-neutral-800 border border-neutral-700 text-sm text-neutral-100 focus:outline-none focus:border-blue-500"
        />
        <button
          onClick={() => search()}
          disabled={loading}
          className="px-4 py-1.5 rounded bg-blue-600 hover:bg-blue-500 text-sm text-white disabled:opacity-50"
        >
          {loading ? '검색 중...' : '검색'}
        </button>
      </div>

      {/* 결과 목록 */}
      {items.length === 0 && !loading ? (
        <p className="text-neutral-400 text-sm">결과 없음</p>
      ) : (
        <div className="space-y-2">
          {items.map((item) => (
            <div
              key={item.repo_id}
              className="rounded-lg border border-neutral-700 bg-neutral-800 p-4 flex items-center justify-between"
            >
              <div className="min-w-0 space-y-1">
                <p className="font-medium truncate">{item.repo_id}</p>
                <div className="flex items-center gap-1.5 flex-wrap">
                  {item.is_base && (
                    <span className="px-1.5 py-0.5 text-[10px] rounded bg-amber-600/30 text-amber-400 font-medium">BASE</span>
                  )}
                  {item.policy_type && (
                    <span className="px-1.5 py-0.5 text-[10px] rounded bg-blue-600/30 text-blue-400">{item.policy_type}</span>
                  )}
                </div>
                <div className="flex gap-3 text-xs text-neutral-400">
                  <span>Downloads: {item.downloads?.toLocaleString() ?? 0}</span>
                  {item.last_modified && <span>{new Date(item.last_modified).toLocaleDateString()}</span>}
                </div>
                {item.base_model && (
                  <div className="text-xs text-neutral-400">
                    베이스 모델: <span className="text-amber-300 font-mono">{item.base_model}</span>
                  </div>
                )}
                {item.datasets && item.datasets.length > 0 && (
                  <div className="text-xs text-neutral-400">
                    학습 데이터: {item.datasets.map((d, i) => (
                      <span key={d} className="text-green-300 font-mono">{i > 0 && ', '}{d}</span>
                    ))}
                  </div>
                )}
                {item.tags.length > 0 && (
                  <div className="flex gap-1 flex-wrap">
                    {item.tags.filter(t => !t.startsWith('dataset:') && !t.endsWith('-policy')).slice(0, 6).map((tag) => (
                      <span key={tag} className="px-1.5 py-0.5 text-[10px] rounded bg-neutral-700 text-neutral-300">{tag}</span>
                    ))}
                  </div>
                )}
              </div>
              <button
                onClick={() => handleDownload(item.repo_id)}
                disabled={downloading.has(item.repo_id)}
                className="ml-4 shrink-0 px-3 py-1.5 text-xs rounded bg-green-600 hover:bg-green-500 text-white disabled:opacity-50"
              >
                {downloading.has(item.repo_id) ? '다운로드 중...' : '다운로드'}
              </button>
            </div>
          ))}
        </div>
      )}
    </div>
  )
}
