import { useState, useEffect } from 'react'
import { api } from '../services/api'
import { useHubDownload, dlActive, bytes, eta } from '../hooks/useHubDownload'

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

/**
 * 다운로드 버튼 + 진행 상황.
 *
 * ⚠ **다섯 상태를 섞지 않는다.** 특히 `incomplete` — 받긴 받았는데 파일이 빠진 것이라
 * 실패가 아니라 **다시 받으면 되는 상태**다. 이걸 완료로 그리면, 몇 주 뒤 영상이
 * 안 나올 때까지 아무도 모른다(2026-09-23 사고).
 */
function DownloadCell({ d, onStart }: { d?: import('../hooks/useHubDownload').HubDownload
                                        onStart: () => void }) {
  const st = d?.status
  if (dlActive(st)) {
    const pct = d?.percent ?? 0
    return (
      <div className="ml-4 shrink-0 w-44">
        <div className="h-1.5 rounded bg-neutral-700 overflow-hidden">
          <div className="h-full bg-green-500 transition-[width] duration-500"
               style={{ width: `${Math.max(2, pct)}%` }} />
        </div>
        <div className="mt-1 text-[10px] text-neutral-400 tabular-nums">
          {st === 'verifying' ? '파일 확인 중…' : (
            <>
              {d!.files_done}/{d!.files_total} · {pct.toFixed(0)}%
              {d!.speed_bps > 0 && <> · {bytes(d!.speed_bps)}/s</>}
              {d!.eta_s != null && d!.eta_s > 0 && <> · {eta(d!.eta_s)}</>}
            </>
          )}
        </div>
      </div>
    )
  }
  if (st === 'completed') {
    return <span className="ml-4 shrink-0 px-3 py-1.5 text-xs text-green-400">받음 ✓</span>
  }
  return (
    <div className="ml-4 shrink-0 text-right">
      <button onClick={onStart}
        className="px-3 py-1.5 text-xs rounded bg-green-600 hover:bg-green-500 text-white">
        {st === 'incomplete' || st === 'error' ? '다시 받기' : '다운로드'}
      </button>
      {st === 'incomplete' && (
        <div className="mt-1 text-[10px] text-amber-300 max-w-44">
          {d!.missing.length}개 파일이 빠졌습니다 — 다시 받으면 빠진 것만 받습니다
        </div>
      )}
      {st === 'error' && (
        <div className="mt-1 text-[10px] text-red-400 max-w-44 break-all">{d!.error}</div>
      )}
    </div>
  )
}

export default function HubBrowser({ type }: Props) {
  const [items, setItems] = useState<HubItem[]>([])
  const [query, setQuery] = useState('')
  const [author, setAuthor] = useState('')
  const [loading, setLoading] = useState(false)
  // ⚠ 예전에는 `Set` 에 **넣기만** 하고 지우는 곳이 없어서 "다운로드 중..." 이
  //   영원히 남고 버튼이 계속 잠겼다. 이제 진행 상황은 훅 한 곳이 본다.
  const { all: dl, start: startDownload } = useHubDownload()
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

  const handleDownload = (repoId: string) =>
    void startDownload(repoId, type === 'models' ? 'model' : 'dataset')

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
              <DownloadCell d={dl[item.repo_id]} onStart={() => handleDownload(item.repo_id)} />
            </div>
          ))}
        </div>
      )}
    </div>
  )
}
