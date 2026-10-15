import { useT } from '../i18n'
import { streamOptionText, type StreamInfo } from '../types/camera'

/**
 * 수집·추론이 쓸 카메라를 **스트림 이름**으로 고르는 `<select>`.
 *
 * - 등록되고 **연결된**(live) 스트림은 고를 수 있다.
 * - 등록됐지만 연결 안 된 것은 **보이되 비활성**이다 — 이유를 같이 적는다.
 * - 저장돼 있던 값이 목록에 없으면(해제된 카메라 등) **숨기지 않고** 비활성으로 보여준다.
 *   조용히 비우면 사람이 "골랐는데 왜 안 되지" 를 모른다.
 *
 * `takenBy(stream)` 은 같은 스트림을 이미 쓰는 다른 키 이름을 돌려준다(없으면 빈 값).
 * 한 스트림을 두 키에 매핑하면 같은 영상이 두 피처가 된다.
 */
export default function CameraStreamSelect({ value, onChange, streams, takenBy, className, placeholder }: {
  value: string
  onChange: (v: string) => void
  streams: StreamInfo[]
  takenBy: (stream: string) => string
  className?: string
  /** 비어 있는 선택지의 글자. 기본은 "카메라 선택..." */
  placeholder?: string
}) {
  const t = useT()
  const known = streams.some((s) => s.stream === value)
  return (
    <select value={value} onChange={(e) => onChange(e.target.value)} className={className}>
      <option value="">{placeholder ?? t('카메라 선택...')}</option>
      {value && !known && (
        <option value={value} disabled>{t('{value} — 목록에 없음 (해제됐거나 이름이 바뀌었습니다)', { value })}</option>
      )}
      {streams.map((s) => {
        const other = takenBy(s.stream)
        const disabled = !s.selectable || !!other
        let text = streamOptionText(s)
        if (!s.selectable) text += ` — ${s.reason}`
        else if (other) text += t(' — {usedBy}에서 사용 중', { usedBy: other })
        else if (s.streaming === false) text += t(' — ⚠ 프레임이 오지 않습니다')
        return <option key={s.stream} value={s.stream} disabled={disabled}>{text}</option>
      })}
    </select>
  )
}

/**
 * 연결 안 된 등록 카메라 — 선택기에서 비활성으로 보이는 것들을 **여기서 바로 연결**한다.
 * 비활성만 있으면 카메라 페이지를 갔다 와야 하고, 재부팅 뒤에는 늘 그 상태다.
 * USB 가 빠진(missing) 것은 연결할 수 없으니 이유만 보여준다.
 */
export function IdleStreams({ streams, connecting, onConnect }: {
  streams: StreamInfo[]
  connecting: string[]
  onConnect: (deviceIds: string[]) => void
}) {
  const t = useT()
  const off = streams.filter((s) => s.state !== 'live')
  if (off.length === 0) return null
  const connectable = off.filter((s) => s.state === 'idle')
  const busy = connectable.some((s) => connecting.includes(s.device_id))
  return (
    <div className="rounded border border-amber-500/30 bg-amber-500/5 p-2 space-y-1">
      <div className="flex items-center gap-2">
        <span className="text-[11px] text-amber-300">{t('연결 안 된 카메라 {length}대', { length: off.length })}</span>
        {connectable.length > 1 && (
          <button disabled={busy} onClick={() => onConnect(connectable.map((s) => s.device_id))}
            className="ml-auto text-[10px] px-1.5 py-0.5 rounded bg-neutral-700 text-neutral-200 hover:bg-neutral-600 disabled:opacity-50">
            {t('모두 연결')}
          </button>
        )}
      </div>
      {off.map((s) => (
        <div key={s.stream} className="flex items-center gap-2 text-xs">
          <span className="font-mono text-neutral-200">{s.stream}</span>
          <span className="text-neutral-500 truncate">{s.reason}</span>
          {s.state === 'idle' && (
            <button disabled={connecting.includes(s.device_id)} onClick={() => onConnect([s.device_id])}
              className="ml-auto text-[10px] px-1.5 py-0.5 rounded bg-green-700 text-white hover:bg-green-600 disabled:opacity-50">
              {connecting.includes(s.device_id) ? t('연결 중...') : t('연결')}
            </button>
          )}
        </div>
      ))}
    </div>
  )
}
