import { useEffect, useState } from 'react'
import { api } from '../services/api'
import { useSystemMessage } from './SystemMessages'

/**
 * 이름·설명 사이드카 편집기 — 데이터셋(`meta/piper_notes.json`)과
 * 모델(`piper_notes.json`) 상세에서 같은 모양으로 쓴다.
 *
 * LeRobot 구조에는 이 자리가 없어서 사이드카로 남긴다(notes_sidecar.py).
 * 대상이 바뀌면 부모가 `key` 로 다시 마운트한다 — 이전 대상의 미저장 초안이
 * 다음 대상에 눌러붙는 사고를 상태 초기화로 막는 가장 단순한 방법이다.
 */

type Notes = { name: string; description: string; updated_at: string }

export default function NotesEditor({ endpoint, source }: {
  /** `/datasets/{id}/notes` 또는 `/models/{id}/notes` */
  endpoint: string
  source: string
}) {
  const { notify } = useSystemMessage()
  const [name, setName] = useState('')
  const [description, setDescription] = useState('')
  const [updatedAt, setUpdatedAt] = useState('')
  const [dirty, setDirty] = useState(false)
  const [saving, setSaving] = useState(false)

  useEffect(() => {
    api.get<Notes>(endpoint)
      .then((n) => { setName(n.name); setDescription(n.description); setUpdatedAt(n.updated_at) })
      .catch(() => {})
  }, [endpoint])

  const save = async () => {
    setSaving(true)
    try {
      const r = await api.put<Notes>(endpoint, { name, description })
      setUpdatedAt(r.updated_at)
      setDirty(false)
      notify({ level: 'info', text: '이름·설명 저장됨', source })
    } catch (e) {
      notify({ level: 'error', source,
        text: e instanceof Error ? e.message : '저장 실패' })
    } finally { setSaving(false) }
  }

  return (
    <div className="space-y-1.5 rounded border border-neutral-700 bg-neutral-900/50 p-3">
      <div className="flex items-center gap-2">
        <input value={name} onChange={(e) => { setName(e.target.value); setDirty(true) }}
          placeholder="이름 (예: 볼트 조립 1차 — 주간 조명)"
          className="flex-1 min-w-0 px-2 py-1 rounded bg-neutral-900 border border-neutral-700
                     text-sm text-neutral-100" />
        <button onClick={() => void save()} disabled={saving || !dirty}
          className="shrink-0 px-3 py-1 text-xs rounded bg-blue-600 hover:bg-blue-500
                     text-white disabled:opacity-40">
          {saving ? '저장 중…' : '저장'}
        </button>
      </div>
      <textarea value={description}
        onChange={(e) => { setDescription(e.target.value); setDirty(true) }}
        placeholder="설명 — 무엇을, 어떤 조건에서 만들었는지. 허브 업로드 시 카드(README)가 됩니다."
        rows={2}
        className="w-full px-2 py-1 rounded bg-neutral-900 border border-neutral-700
                   text-xs text-neutral-200" />
      {updatedAt && !dirty && (
        <p className="text-[10px] text-neutral-500">마지막 수정 {updatedAt.slice(0, 16).replace('T', ' ')}</p>
      )}
    </div>
  )
}
