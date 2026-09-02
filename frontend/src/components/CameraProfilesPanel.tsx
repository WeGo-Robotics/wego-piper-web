import { useCallback, useEffect, useState } from 'react'
import { api } from '../services/api'
import { useSystemMessage } from './SystemMessages'

/**
 * 카메라 프로파일 탭 — 목록·캡처·적용·활성 지정 + **값 상세 편집**.
 *
 * 캡처(장치 → 값)만 있던 프로파일에 편집(값 → 저장)이 붙는다: 노출 하나만
 * 고치고 싶을 때 장치를 다시 맞춰 통째로 재캡처하는 것은 일이 아니라 벌이다.
 *
 * ⚠ **적용은 저장된 값 기준이다.** 편집 중(dirty)에는 적용 버튼이 그걸
 * 말해준다 — 화면의 미저장 값이 장치로 가는 척하면 "고쳤는데 왜 그대로지"가 된다.
 *
 * 값의 뜻은 백엔드와 같다 (`camera_profiles.py`): 항목(entry)은 카메라 한 대,
 * `controls` 는 이름→숫자, `match` 는 어느 장치에 붙일지의 단서다. `match` 는
 * 여기서 안 고친다 — 손으로 고칠 물건이 아니라 재캡처로 다시 만드는 것이다.
 */

type CamApplyResult = {
  cam_id: string; display_name: string
  applied: number; locked: number; failed: number; skipped: number
  truncated?: boolean
  details?: { name: string; want: number; got: number | null; status: string }[]
}

type ProfileReport = {
  profile: string
  cameras: CamApplyResult[]
  unmatched?: string[]
  error?: string
}

/** 프로파일 적용 결과 한 줄. 데몬이 세어 준 것을 그대로 보여준다.
 *  `잠김`은 실패가 아니다 — 자동 노출이 켜져 있어 그 값이 지금 안 쓰이는 상태다. */
function ApplyBadge({ r }: { r: CamApplyResult }) {
  const bad = r.failed > 0
  return (
    <span
      title={(r.details ?? []).map((d) => `${d.name}: ${d.want} → ${d.got ?? '—'} (${d.status})`).join('\n')}
      className={`rounded px-1.5 py-0.5 text-[10px] ${bad
        ? 'bg-red-500/15 text-red-400' : 'bg-neutral-700/60 text-neutral-300'}`}
    >
      {r.display_name}: 적용 {r.applied}
      {r.locked ? ` / 잠김 ${r.locked}` : ''}
      {r.failed ? ` / 실패 ${r.failed}` : ''}
      {r.truncated ? ' / 시간초과' : ''}
    </span>
  )
}

type PresetMeta = { name: string; note: string; updated_at: string }

type ProfileEntry = {
  key: string
  match: Record<string, string | null | undefined>
  stream: { width?: number | null; height?: number | null; fps?: number | null; fourcc?: string | null }
  /** 편집 중에는 문자열도 허용 — 숫자 입력을 지우는 순간이 있다. 저장 때 검증한다. */
  controls: Record<string, number | string>
}

type ProfileDetail = {
  name: string; values: { cameras: ProfileEntry[] }
  scope: string; note: string; updated_at: string
}

const when = (iso: string) => iso ? iso.slice(0, 16).replace('T', ' ') : ''

export default function CameraProfilesPanel({ active, onActiveChange }: {
  active: string
  onActiveChange: (name: string) => void
}) {
  const { notify, confirm: askConfirm } = useSystemMessage()
  const say = (text: string) => notify({ level: 'info', text, source: '프로파일' })
  const sayError = (text: string) => notify({ level: 'error', text, source: '프로파일' })

  const [metas, setMetas] = useState<PresetMeta[]>([])
  const [selected, setSelected] = useState<string | null>(null)
  const [detail, setDetail] = useState<ProfileDetail | null>(null)
  const [entries, setEntries] = useState<ProfileEntry[]>([])
  const [noteDraft, setNoteDraft] = useState('')
  const [dirty, setDirty] = useState(false)
  const [captureName, setCaptureName] = useState('')
  const [report, setReport] = useState<ProfileReport | null>(null)
  const [busy, setBusy] = useState(false)
  // 재현성 검토 (camera-profiles.md 검토 절) — 판정은 백엔드 순수 함수가 한다.
  // 편집 중인 값(미저장 포함)을 멈춘 뒤 보내 경고를 받아 온다.
  const [checkWarns, setCheckWarns] = useState<{ key: string; text: string }[]>([])
  useEffect(() => {
    if (!detail) { setCheckWarns([]); return }
    const t = setTimeout(() => {
      api.post<{ warnings: { key: string; text: string }[] }>(
        '/cameras/profiles/validate', { cameras: entries })
        .then((r) => setCheckWarns(r.warnings))
        .catch(() => {})
    }, 400)
    return () => clearTimeout(t)
  }, [detail, entries])

  const refresh = useCallback(() => {
    api.get<PresetMeta[]>('/presets/camera').then(setMetas).catch(() => setMetas([]))
  }, [])
  useEffect(() => { refresh() }, [refresh])

  const select = async (name: string) => {
    if (dirty && !(await askConfirm(
      `"${selected}" 에 저장 안 된 수정이 있습니다. 버리고 이동할까요?`))) return
    try {
      const d = await api.get<ProfileDetail>(`/presets/camera/${encodeURIComponent(name)}`)
      setSelected(name)
      setDetail(d)
      // 깊은 복사 — 원본을 들고 있어야 "저장 전" 과 비교할 수 있다
      setEntries(JSON.parse(JSON.stringify(d.values?.cameras ?? [])))
      setNoteDraft(d.note ?? '')
      setDirty(false)
      setReport(null)
    } catch (e) {
      sayError(e instanceof Error ? e.message : '프로파일을 읽지 못했습니다')
    }
  }

  const edit = (fn: (prev: ProfileEntry[]) => ProfileEntry[]) => {
    setEntries(fn)
    setDirty(true)
  }

  const save = async () => {
    if (!detail) return
    // 문자열로 남은 컨트롤 값(입력을 지운 채)은 저장 전에 잡는다 —
    // 조용히 0 으로 저장하면 다음 연결 때 그 0 이 장치로 들어간다
    const cleaned: ProfileEntry[] = []
    for (const e of entries) {
      const controls: Record<string, number> = {}
      for (const [k, v] of Object.entries(e.controls)) {
        const n = typeof v === 'number' ? v : parseFloat(v)
        if (Number.isNaN(n)) { sayError(`"${e.key}" 의 ${k} 값이 비었습니다`); return }
        controls[k] = n
      }
      cleaned.push({ ...e, controls })
    }
    setBusy(true)
    try {
      await api.post('/presets/camera', {
        name: detail.name, values: { cameras: cleaned },
        scope: detail.scope || 'device', note: noteDraft,
      })
      say(`"${detail.name}" 저장됨`)
      setDirty(false)
      refresh()
      await select(detail.name)
    } catch (e) {
      sayError(e instanceof Error ? e.message : '저장 실패')
    } finally { setBusy(false) }
  }

  /** 수동 적용 — 연결 시 자동 적용과 같은 데몬 함수를 탄다. 활성은 안 바꾼다. */
  const applyNow = async (name: string) => {
    setBusy(true)
    try {
      const r = await api.post<ProfileReport>('/cameras/profiles/apply', { name })
      setReport(r)
      if (r.error) { sayError(r.error); return }
      const sum = r.cameras.reduce((a, c) => ({
        applied: a.applied + c.applied, locked: a.locked + c.locked, failed: a.failed + c.failed,
      }), { applied: 0, locked: 0, failed: 0 })
      const miss = r.unmatched?.length ? ` / 못 찾음 ${r.unmatched.length}대` : ''
      say(`"${name}" 적용 — ${sum.applied} 적용 / ${sum.locked} 잠김 / ${sum.failed} 실패${miss}`)
    } catch (e) {
      sayError(e instanceof Error ? e.message : '적용 실패')
    } finally { setBusy(false) }
  }

  const makeActive = async (name: string) => {
    try {
      await api.post('/cameras/profiles/active', { name })
      onActiveChange(name)
      say(`활성 프로파일: ${name} — 이제부터 카메라를 열 때마다 적용됩니다`)
    } catch (e) { sayError(e instanceof Error ? e.message : '활성 지정 실패') }
  }

  const capture = async () => {
    const name = captureName.trim()
    if (!name) return
    setBusy(true)
    try {
      const r = await api.post<{
        values: { cameras: unknown[] }
        warnings?: { key: string; text: string }[]
      }>('/cameras/profiles/capture', { name })
      // 캡처는 백엔드가 활성 지정까지 한다 (기존 동작 유지)
      onActiveChange(name)
      say(`"${name}" 캡처 — 카메라 ${r.values?.cameras?.length ?? 0}대의 현재 장치값 (활성으로 지정됨)`)
      // AE 켠 채 캡처한 걸 몇 주 뒤 데이터가 안 맞을 때에야 알면 최악이다 —
      // 캡처한 그 자리에서 말한다
      for (const w of r.warnings ?? [])
        notify({ level: 'warn', text: `${w.key}: ${w.text}`, source: '프로파일' })
      setCaptureName('')
      refresh()
      await select(name)
    } catch (e) {
      sayError(e instanceof Error ? e.message : '캡처 실패')
    } finally { setBusy(false) }
  }

  const remove = async (name: string) => {
    if (!(await askConfirm(`프로파일 "${name}" 을(를) 삭제할까요?\n수집·추론 화면에 이 이름이 지정돼 있으면 그쪽 시작이 거부됩니다.`)))
      return
    try {
      await api.delete(`/presets/camera/${encodeURIComponent(name)}`)
      if (active === name) {
        // 지운 이름이 활성으로 남으면 연결 때마다 "프로파일 없음" 경고가 반복된다
        await api.post('/cameras/profiles/active', { name: '' }).catch(() => {})
        onActiveChange('')
      }
      if (selected === name) { setSelected(null); setDetail(null); setEntries([]); setDirty(false) }
      say(`"${name}" 삭제됨`)
      refresh()
    } catch (e) { sayError(e instanceof Error ? e.message : '삭제 실패') }
  }

  const matchSummary = (e: ProfileEntry) => {
    const m = e.match || {}
    return [m.cam_type, m.name, m.serial || m.usb_port, m.last_dev]
      .filter(Boolean).join(' · ')
  }

  return (
    <div className="grid grid-cols-1 lg:grid-cols-[280px_1fr] gap-4 items-start">
      {/* ── 왼쪽: 목록 + 캡처 ── */}
      <div className="space-y-3">
        <div className="rounded-lg border border-neutral-700 bg-neutral-800 divide-y divide-neutral-700/60">
          {metas.length === 0 && (
            <p className="p-3 text-xs text-neutral-500">
              프로파일이 없습니다 — 장치 탭에서 노출·WB 를 맞춘 뒤 아래에서 캡처하세요.
            </p>
          )}
          {metas.map((m) => (
            <button key={m.name} onClick={() => void select(m.name)}
              className={`block w-full px-3 py-2 text-left hover:bg-neutral-700/40 ${
                selected === m.name ? 'bg-neutral-700/60' : ''}`}>
              <span className="flex items-center gap-2 text-sm">
                <span className="truncate text-neutral-100">{m.name}</span>
                {active === m.name && (
                  <span className="shrink-0 rounded bg-green-500/15 px-1.5 text-[10px] text-green-400"
                        title="카메라를 열 때마다 자동 적용되는 프로파일">활성</span>
                )}
              </span>
              <span className="block text-[10px] text-neutral-500">
                {when(m.updated_at)}{m.note ? ` — ${m.note}` : ''}
              </span>
            </button>
          ))}
        </div>

        <div className="rounded-lg border border-neutral-700 bg-neutral-800 p-3 space-y-2">
          <p className="text-xs text-neutral-400">현재 장치값으로 캡처</p>
          <div className="flex gap-2">
            <input value={captureName} onChange={(e) => setCaptureName(e.target.value)}
              placeholder="이름 (주간/야간/형광등…)"
              className="flex-1 min-w-0 px-2 py-1 rounded bg-neutral-900 border border-neutral-700 text-sm" />
            <button onClick={() => void capture()} disabled={busy || !captureName.trim()}
              className="px-3 py-1 text-sm rounded bg-blue-600 hover:bg-blue-500 text-white disabled:opacity-50">
              캡처
            </button>
          </div>
          <p className="text-[10px] text-neutral-500">
            화면 상태가 아니라 <b>지금 장치에 들어 있는 값</b>을 읽어 담고, 활성으로 지정합니다.
          </p>
        </div>
      </div>

      {/* ── 오른쪽: 상세 편집 ── */}
      {!detail ? (
        <p className="rounded-lg border border-neutral-800 bg-neutral-900/40 p-6 text-sm text-neutral-500">
          왼쪽에서 프로파일을 선택하면 카메라별 값을 보고 고칠 수 있습니다.
        </p>
      ) : (
        <div className="space-y-3">
          <div className="flex flex-wrap items-center gap-2">
            <h2 className="text-lg font-semibold">{detail.name}</h2>
            <span className="text-[10px] text-neutral-500">{when(detail.updated_at)}</span>
            <div className="ml-auto flex gap-2">
              <button onClick={() => void applyNow(detail.name)} disabled={busy}
                title={dirty ? '적용은 저장된 값 기준입니다 — 먼저 저장하세요' : '지금 연결된 카메라에 밀어 넣습니다'}
                className="px-3 py-1 text-sm rounded bg-neutral-700 hover:bg-neutral-600 disabled:opacity-50">
                지금 적용{dirty ? ' (저장 전 값)' : ''}
              </button>
              <button onClick={() => void makeActive(detail.name)} disabled={active === detail.name}
                className="px-3 py-1 text-sm rounded bg-neutral-700 hover:bg-neutral-600 disabled:opacity-50">
                {active === detail.name ? '활성임' : '활성으로 지정'}
              </button>
              <button onClick={() => void remove(detail.name)}
                className="px-3 py-1 text-sm rounded bg-red-600/80 hover:bg-red-500 text-white">
                삭제
              </button>
            </div>
          </div>

          {report && report.cameras.length > 0 && (
            <div className="flex flex-wrap gap-1">
              {report.cameras.map((r) => <ApplyBadge key={r.cam_id} r={r} />)}
            </div>
          )}

          {checkWarns.length > 0 && (
            <div className="rounded border border-amber-500/40 bg-amber-950/40 px-3 py-2 space-y-0.5">
              <p className="text-[10px] font-semibold text-amber-300">재현성 검토</p>
              {checkWarns.map((w, i) => (
                <p key={i} className="text-[11px] text-amber-200">
                  <span className="font-mono">{w.key}</span> — {w.text}
                </p>
              ))}
            </div>
          )}

          <input value={noteDraft}
            onChange={(e) => { setNoteDraft(e.target.value); setDirty(true) }}
            placeholder="메모 (조명 조건 등)"
            className="w-full px-2 py-1.5 rounded bg-neutral-900 border border-neutral-700 text-sm" />

          {entries.map((entry, ei) => (
            <div key={ei} className="rounded-lg border border-neutral-700 bg-neutral-800 p-3 space-y-2">
              <div className="flex items-center gap-2">
                <span className="font-mono text-sm text-neutral-100">{entry.key || '(키 없음)'}</span>
                <span className="truncate text-[10px] text-neutral-500" title={matchSummary(entry)}>
                  {matchSummary(entry)}
                </span>
                <button onClick={() => {
                  void (async () => {
                    if (await askConfirm(`"${entry.key}" 항목을 프로파일에서 뺄까요?\n이 카메라에는 더 이상 값이 적용되지 않습니다.`))
                      edit((prev) => prev.filter((_, i) => i !== ei))
                  })()
                }} className="ml-auto text-xs text-neutral-500 hover:text-red-400">항목 삭제</button>
              </div>

              {/* ⚠ 스트림은 **기록용이다.** 연결 해상도는 시작 요청(prepare_cameras)이
                  정하고, 프로파일의 이 값을 읽는 코드는 아직 없다 (검토 G4) —
                  소비를 배선하기 전까지 화면이 그 사실을 말해준다. */}
              <div className="flex flex-wrap items-center gap-2 text-xs">
                <span className="text-neutral-500"
                      title="캡처 당시의 해상도·fps 기록입니다. 연결에 쓰는 값은 시작 요청이 정합니다 — 이 값은 아직 어디에도 적용되지 않습니다.">
                  스트림 (기록용)</span>
                {(['width', 'height', 'fps'] as const).map((f) => (
                  <label key={f} className="flex items-center gap-1">
                    <span className="text-neutral-500">{f === 'width' ? 'W' : f === 'height' ? 'H' : 'fps'}</span>
                    <input type="number" value={entry.stream?.[f] ?? ''}
                      onChange={(e) => {
                        const v = e.target.value === '' ? null : Number(e.target.value)
                        edit((prev) => prev.map((x, i) => i === ei
                          ? { ...x, stream: { ...x.stream, [f]: v } } : x))
                      }}
                      className="w-16 px-1 py-0.5 rounded bg-neutral-900 border border-neutral-700 text-center" />
                  </label>
                ))}
                <label className="flex items-center gap-1">
                  <span className="text-neutral-500">fourcc</span>
                  <input value={entry.stream?.fourcc ?? ''}
                    onChange={(e) => edit((prev) => prev.map((x, i) => i === ei
                      ? { ...x, stream: { ...x.stream, fourcc: e.target.value || null } } : x))}
                    className="w-16 px-1 py-0.5 rounded bg-neutral-900 border border-neutral-700 text-center font-mono" />
                </label>
              </div>

              {/* 컨트롤 — 이 프로파일의 본체다 */}
              <div className="space-y-1">
                {Object.entries(entry.controls).map(([cname, cval]) => (
                  <div key={cname} className="flex items-center gap-2 text-xs">
                    <span className="w-52 truncate font-mono text-neutral-300" title={cname}>{cname}</span>
                    <input type="number" step="any" value={cval}
                      onChange={(e) => edit((prev) => prev.map((x, i) => i === ei
                        ? { ...x, controls: { ...x.controls, [cname]: e.target.value } } : x))}
                      className="w-28 px-1.5 py-0.5 rounded bg-neutral-900 border border-neutral-700 text-right tabular-nums" />
                    <button onClick={() => edit((prev) => prev.map((x, i) => {
                      if (i !== ei) return x
                      const { [cname]: _drop, ...rest } = x.controls
                      return { ...x, controls: rest }
                    }))} className="text-neutral-500 hover:text-red-400" title="이 컨트롤을 프로파일에서 뺀다">✕</button>
                  </div>
                ))}
                {Object.keys(entry.controls).length === 0 && (
                  <p className="text-[10px] text-neutral-500">컨트롤 없음 — 캡처 때 default 와 같은 값은 저장되지 않습니다.</p>
                )}
                <AddControlRow onAdd={(name, value) => edit((prev) => prev.map((x, i) => i === ei
                  ? { ...x, controls: { ...x.controls, [name]: value } } : x))} />
              </div>
            </div>
          ))}

          <div className="flex items-center gap-3">
            <button onClick={() => void save()} disabled={busy || !dirty}
              className="px-4 py-1.5 text-sm rounded bg-blue-600 hover:bg-blue-500 text-white disabled:opacity-50">
              저장
            </button>
            {dirty
              ? <span className="text-xs text-amber-400">저장 안 된 수정이 있습니다 — 적용·연결은 저장된 값을 씁니다</span>
              : <span className="text-xs text-neutral-500">저장된 값 그대로입니다</span>}
          </div>
        </div>
      )}
    </div>
  )
}

/** 컨트롤 추가 한 줄. 이름은 장치 컨트롤 이름과 같아야 적용된다 —
 *  장치 탭의 설정 창에서 이름을 확인할 수 있다. */
function AddControlRow({ onAdd }: { onAdd: (name: string, value: number) => void }) {
  const [name, setName] = useState('')
  const [value, setValue] = useState('')
  const ok = name.trim() !== '' && !Number.isNaN(parseFloat(value))
  return (
    <div className="flex items-center gap-2 pt-1 text-xs">
      <input value={name} onChange={(e) => setName(e.target.value)}
        placeholder="컨트롤 이름 (예: exposure)"
        className="w-52 px-1.5 py-0.5 rounded bg-neutral-900 border border-neutral-700 font-mono" />
      <input type="number" step="any" value={value} onChange={(e) => setValue(e.target.value)}
        placeholder="값"
        className="w-28 px-1.5 py-0.5 rounded bg-neutral-900 border border-neutral-700 text-right" />
      <button disabled={!ok}
        onClick={() => { onAdd(name.trim(), parseFloat(value)); setName(''); setValue('') }}
        className="px-2 py-0.5 rounded bg-neutral-700 hover:bg-neutral-600 disabled:opacity-40">
        + 추가
      </button>
    </div>
  )
}
