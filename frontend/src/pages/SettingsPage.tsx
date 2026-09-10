import { useEffect, useState, useCallback } from 'react'
import FloorGuardPanel from '../components/FloorGuardPanel'
import HfAccountPanel from '../components/HfAccountPanel'
import ServicesPanel from '../components/ServicesPanel'
import VersionCard from '../components/VersionCard'
import { useSystemMessage } from '../components/SystemMessages'
import { api } from '../services/api'

/**
 * 설정 — 탭으로 나눈다.
 *
 * 한 장에 계속 쌓으면 스크롤로 찾게 되고, 서로 상관없는 것들이 섞인다.
 * 탭 목록은 **여기 한 곳**에서 나온다 — `pages.ts` 가 페이지에 대해 하는 일과 같다.
 */

const TABS = [
  { id: 'general', label: '일반' },
  { id: 'safety', label: '안전' },
  { id: 'services', label: '서비스' },
  { id: 'hub', label: '저장소' },
] as const
type TabId = (typeof TABS)[number]['id']

type ModelPath = { path: string; exists: boolean }

export default function SettingsPage() {
  // ⚠ `window.alert` 를 쓰지 않는다 — 이벤트 루프를 막아 E-stop heartbeat 가
  //   끊기고, 2초 타임아웃에 추론이 강제 종료된다 (confirm 으로 실제로 겪었다).
  const { notify } = useSystemMessage()
  const notifyError = (text: string) =>
    notify({ level: 'error', text, source: '설정' })
  const [tab, setTab] = useState<TabId>('general')
  const [paths, setPaths] = useState<ModelPath[]>([])
  const [newPath, setNewPath] = useState('')

  const fetchPaths = useCallback(() => {
    api.get<ModelPath[]>('/models/paths').then(setPaths).catch(() => {})
  }, [])

  useEffect(() => { fetchPaths() }, [fetchPaths])

  // 디스크 경고 임계치 — 상태바 💾 칩의 노란 ⚠ 기준(데이터셋+모델 사용량 합)
  const [diskThreshold, setDiskThreshold] = useState('')
  const [diskTotal, setDiskTotal] = useState<number | null>(null)
  useEffect(() => {
    api.get<{ threshold_gb: number; total_gb: number }>('/datasets/disk-usage')
      .then((r) => { setDiskThreshold(String(r.threshold_gb)); setDiskTotal(r.total_gb) })
      .catch(() => {})
  }, [])
  const saveDiskThreshold = async () => {
    const v = Number(diskThreshold)
    if (!Number.isFinite(v) || v <= 0) { notifyError('0보다 큰 GB 값을 입력하세요'); return }
    try {
      const r = await api.post<{ threshold_gb: number }>('/datasets/disk-threshold', { threshold_gb: v })
      setDiskThreshold(String(r.threshold_gb))
      notify({ level: 'info', source: '설정',
               text: `디스크 경고 임계치 ${r.threshold_gb}GB 저장됨 — 상태바 💾 에 바로 반영됩니다` })
    } catch (e) {
      notifyError(e instanceof Error ? e.message : '저장 실패')
    }
  }

  const handleAdd = async () => {
    const trimmed = newPath.trim()
    if (!trimmed) return
    try {
      await api.post('/models/paths', { path: trimmed })
      setNewPath('')
      fetchPaths()
    } catch (e) {
      notifyError(e instanceof Error ? e.message : '경로 추가 실패')
    }
  }

  const handleRemove = async (path: string) => {
    if (paths.length <= 1) { notifyError('최소 1개의 경로가 필요합니다'); return }
    await api.post('/models/paths/remove', { path })
    fetchPaths()
  }

  return (
    <div className="space-y-6">
      <h1 className="text-2xl font-bold">설정</h1>

      <div className="flex gap-1 border-b border-neutral-700">
        {TABS.map((t) => (
          <button key={t.id} onClick={() => setTab(t.id)}
            className={`-mb-px border-b-2 px-3 py-1.5 text-sm transition-colors ${
              tab === t.id
                ? 'border-blue-500 text-white'
                : 'border-transparent text-neutral-400 hover:text-neutral-200'}`}>
            {t.label}
          </button>
        ))}
      </div>

      {tab === 'safety' && (
        <div className="rounded-lg border border-neutral-700 bg-neutral-800 p-5">
          <FloorGuardPanel />
        </div>
      )}

      {tab === 'services' && (<>
        <div className="rounded-lg border border-neutral-700 bg-neutral-800 p-5">
          <VersionCard />
        </div>
        <div className="rounded-lg border border-neutral-700 bg-neutral-800 p-5">
          <ServicesPanel />
        </div>
      </>)}

      {tab === 'hub' && <HfAccountPanel />}

      {tab === 'general' && (<>
      <div className="rounded-lg border border-neutral-700 bg-neutral-800 p-5 space-y-3">
        <div>
          <h2 className="text-lg font-semibold">디스크 경고 임계치</h2>
          <p className="text-xs text-neutral-400 mt-1">
            데이터셋·모델 사용량 합이 이 값을 넘으면 상태바 💾 에 ⚠ 가 붙습니다.
            {diskTotal != null && <> 지금 사용량: <b>{diskTotal}GB</b></>}
          </p>
        </div>
        <div className="flex items-center gap-2">
          <input type="number" min="1" step="1" value={diskThreshold}
            onChange={(e) => setDiskThreshold(e.target.value)}
            onKeyDown={(e) => e.key === 'Enter' && saveDiskThreshold()}
            className="w-32 px-3 py-2 rounded bg-neutral-900 border border-neutral-700 text-sm text-neutral-100 text-right tabular-nums focus:outline-none focus:border-blue-500" />
          <span className="text-sm text-neutral-400">GB</span>
          <button onClick={saveDiskThreshold}
            className="px-4 py-2 text-sm rounded bg-blue-600 hover:bg-blue-500 text-white">저장</button>
        </div>
        <p className="text-[10px] text-neutral-500">
          저장값은 파일로 남아 재시작·환경변수(PIPER_DISK_WARNING_THRESHOLD_GB)보다 우선합니다.
        </p>
      </div>

      <div className="rounded-lg border border-neutral-700 bg-neutral-800 p-5 space-y-4">
        <div>
          <h2 className="text-lg font-semibold">모델 검색 경로</h2>
          <p className="text-xs text-neutral-400 mt-1">추론에 사용할 모델 체크포인트를 검색하는 디렉토리 목록입니다.</p>
        </div>

        <div className="space-y-2">
          {paths.map((p) => (
            <div key={p.path} className="flex items-center gap-3 rounded-lg border border-neutral-700 bg-neutral-900 px-4 py-2.5">
              <span className={`w-2 h-2 rounded-full flex-shrink-0 ${p.exists ? 'bg-green-500' : 'bg-red-500'}`} />
              <span className="flex-1 font-mono text-sm text-neutral-200 truncate">{p.path}</span>
              {!p.exists && <span className="text-xs text-red-400 flex-shrink-0">경로 없음</span>}
              <button onClick={() => handleRemove(p.path)}
                className="text-neutral-500 hover:text-red-400 text-sm flex-shrink-0 px-2">삭제</button>
            </div>
          ))}
        </div>

        <div className="flex gap-2">
          <input type="text" value={newPath} onChange={(e) => setNewPath(e.target.value)}
            onKeyDown={(e) => e.key === 'Enter' && handleAdd()}
            placeholder="경로 추가 (예: /home/user/models)"
            className="flex-1 px-3 py-2 rounded bg-neutral-900 border border-neutral-700 text-sm text-neutral-100 placeholder:text-neutral-500 focus:outline-none focus:border-blue-500" />
          <button onClick={handleAdd}
            className="px-4 py-2 text-sm rounded bg-blue-600 hover:bg-blue-500 text-white">추가</button>
        </div>
      </div>
      </>)}
    </div>
  )
}
