import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { api } from '../services/api'

/**
 * 가상환경 편집기 — 테이블 위 사물을 사람이 올린다 (feature/sim-scene-editor.md §8).
 *
 * **독립 페이지다** (사용자 결정 2026-09-17). 설정 탭이나 로봇 카드에 끼워 넣지 않는다:
 * 편집은 작업이지 설정이 아니고, 가상환경은 로봇에 딸린 속성이 아니라 여러 개를 만들고 고른다.
 *
 * 정본은 **가상환경 JSON** 이다. 이 화면은 그 파일을 고치는 폼일 뿐이고, 굽는 일은 simd 가
 * 한다. 그래서 "환경 불러오기/내보내기"가 파일 하나를 주고받는 일이 된다.
 *
 * ⚠ 배치 화면은 **적용된 가상환경**의 세계를 본다(시뮬 탑뷰 그대로). 편집 중인 가상환경이 아직
 * 안 올라갔으면 클릭해도 엉뚱한 세계를 건드리게 되므로 그때는 배치를 막고 그렇게 말한다.
 */

type SceneRow = {
  id: string; name: string; count: number; applied: boolean; updated_at: number
  error: string | null; missing_assets?: string[]
}
type Obj = {
  id: string; label: string; shape: string; movable: boolean
  pos: number[]; euler_deg: number[]; rgba: number[]
  size?: number[]; params?: Record<string, number | number[]>
  asset?: string; scale?: number          // shape === 'mesh' 일 때
  mass?: number; friction: number[]; condim: number; solref: number[]
}
type Spec = { version: number; id: string; name: string; objects: Obj[] }
type Asset = {
  id: string; name: string; format: string; unit_scale: number; bbox_m: number[]; bytes: number
  error?: string | null
  raw: { vertices: number; faces: number; bbox: number[]; volume: number; concavity: number; closed: boolean }
}
type Defaults = {
  primitives: Record<string, { size_len: number }>
  presets: Record<string, { params: Record<string, number | number[]> }>
  reserved: string[]
  max_objects: number
  movable_physics: { friction: number[]; condim: number; solref: number[] }
  static_physics: { friction: number[]; condim: number; solref: number[] }
}

const SHAPE_KO: Record<string, string> = {
  box: '상자', sphere: '구', cylinder: '원기둥', capsule: '캡슐', ellipsoid: '타원체',
  'preset:bin': '통 (조립)', mesh: '메시',
}
//: 단위 — OBJ·STL 에는 단위가 없다. 사람이 고르는 값이다.
const UNITS: [number, string][] = [[1, 'm (미터)'], [0.01, 'cm (센티)'], [0.001, 'mm (밀리)']]
const mm = (v: number) => (v * 1000).toFixed(v * 1000 < 10 ? 1 : 0)
//: 모양별 size 라벨 — MuJoCo 의 size 는 **반지름·반변**이다. 그 말을 화면에 적어야
//  사람이 2cm 블럭을 만들 때 0.02 를 넣는다(0.04 가 아니라).
const SIZE_LABELS: Record<string, string[]> = {
  box: ['반변 X', '반변 Y', '반변 Z'],
  sphere: ['반지름'],
  cylinder: ['반지름', '반높이'],
  capsule: ['반지름', '반높이'],
  ellipsoid: ['반지름 X', '반지름 Y', '반지름 Z'],
}

/** 물체를 구하는 곳 — **링크는 전부 확인했다**(2026-09-17). 공식 YCB 사이트
 *  (ycbbenchmarks.com)는 그때 500 이라 내려받기 도구를 대신 건다. */
const SOURCES: { group: string; note: string; items: [string, string, string][] }[] = [
  {
    group: '가져오기 — 이미 만들어진 것',
    note: 'MuJoCo 로 바로 쓸 수 있는 것부터 본다. 라이선스는 저마다 다르니 쓰기 전에 확인하세요.',
    items: [
      ['MuJoCo Scanned Objects', 'https://github.com/kevinzakka/mujoco_scanned_objects',
       'Google 이 스캔한 생활용품 1000여 종을 MuJoCo 용으로 변환해 둔 것 — 변환이 필요 없다'],
      ['MuJoCo Menagerie', 'https://github.com/google-deepmind/mujoco_menagerie',
       'DeepMind 의 정식 MJCF 모음. 로봇 위주지만 소품도 있다'],
      ['YCB 내려받기 도구', 'https://github.com/sea-bass/ycb-tools',
       '조작 연구의 표준 물체 77종(머스터드·크래커 상자·바나나·머그…). 공식 사이트가 자주 죽어 이 도구가 편하다'],
      ['Objaverse', 'https://objaverse.allenai.org/',
       '수십만 종. GLB 라 변환이 필요하고 품질 편차가 크다'],
      ['Sketchfab 무료 모델', 'https://sketchfab.com/features/free-3d-models',
       '낱개로 구할 때. OBJ 로 내려받고 라이선스를 꼭 본다'],
    ],
  },
  {
    group: '실물을 뜨기 — 폰으로 스캔',
    note: '진짜 쓸 물건을 그대로 뜬다. 시뮬과 실기 차이를 줄이는 데 제일 유리하다.',
    items: [
      ['Polycam', 'https://poly.cam/', 'iOS·Android. OBJ 로 내보내기'],
      ['Scaniverse', 'https://scaniverse.com/', '무료. iPhone LiDAR 로 작은 물건도 잘 뜬다'],
      ['KIRI Engine', 'https://www.kiriengine.app/', 'LiDAR 없는 폰도 사진으로 뜬다'],
    ],
  },
  {
    group: '직접 만들기 · 손보기',
    note: '스캔은 단위·원점·면 수가 제각각이라 한 번은 손봐야 한다.',
    items: [
      ['Blender', 'https://www.blender.org/',
       '무료. 데시메이트로 면 줄이기, 원점 옮기기, OBJ/바이너리 STL 내보내기'],
      ['obj2mjcf', 'https://github.com/kevinzakka/obj2mjcf',
       'OBJ → MuJoCo 조각. 오목한 것을 볼록 조각으로 쪼개 준다'],
    ],
  },
]

const hex = (rgba: number[]) =>
  '#' + rgba.slice(0, 3).map((v) => Math.round(Math.max(0, Math.min(1, v)) * 255).toString(16).padStart(2, '0')).join('')
const fromHex = (h: string) => [1, 3, 5].map((i) => parseInt(h.slice(i, i + 2), 16) / 255).concat([1])

function Num({ label, value, step = 0.005, onChange }: {
  label: string; value: number; step?: number; onChange: (v: number) => void
}) {
  return (
    <label className="flex flex-col gap-0.5">
      <span className="text-[10px] text-neutral-500">{label}</span>
      <input type="number" step={step} value={value}
        onChange={(e) => onChange(Number(e.target.value))}
        className="w-full rounded bg-neutral-900 border border-neutral-700 px-1.5 py-1 text-xs font-mono" />
    </label>
  )
}

export default function ScenePage() {
  const [rows, setRows] = useState<SceneRow[]>([])
  const [current, setCurrent] = useState<string | null>(null)
  const [sid, setSid] = useState('')
  const [spec, setSpec] = useState<Spec | null>(null)
  const [dirty, setDirty] = useState(false)
  const [sel, setSel] = useState('')
  const [defs, setDefs] = useState<Defaults | null>(null)
  const [err, setErr] = useState('')
  const [busy, setBusy] = useState('')
  const [tick, setTick] = useState(0)          // 탑뷰 스냅샷 폴링
  const [assets, setAssets] = useState<Asset[]>([])
  const fileRef = useRef<HTMLInputElement | null>(null)
  const meshRef = useRef<HTMLInputElement | null>(null)

  const applied = !!spec && current === sid && !dirty

  const reload = useCallback(async () => {
    const r = await api.get<{ scenes: SceneRow[]; current: string | null }>('/sim/scenes')
    setRows(r.scenes); setCurrent(r.current)
    return r
  }, [])

  const loadAssets = useCallback(() => api.get<{ assets: Asset[] }>('/sim/assets')
    .then((r) => setAssets(r.assets)).catch(() => {}), [])

  useEffect(() => {
    api.get<Defaults>('/sim/scenes/defaults').then(setDefs).catch(() => {})
    void loadAssets()
    reload().then((r) => {
      const first = r.current ?? r.scenes.find((s) => !s.error)?.id ?? ''
      if (first) setSid(first)
    }).catch((e) => setErr(e instanceof Error ? e.message : '목록 실패'))
  }, [reload, loadAssets])

  // 가상환경 선택 → 명세를 읽어 폼으로
  useEffect(() => {
    if (!sid) { setSpec(null); return }
    api.get<Spec>(`/sim/scenes/${sid}`)
      .then((s) => { setSpec(s); setDirty(false); setSel(s.objects[0]?.id ?? '') })
      .catch((e) => setErr(e instanceof Error ? e.message : '가상환경을 못 읽었습니다'))
  }, [sid])

  // 탑뷰 — 시뮬 카메라는 연결이 멱등·빠르다(실기 프로브 없음)
  useEffect(() => {
    api.post('/cameras/connect', { id: 'sim:top' }).catch(() => {})
    const iv = window.setInterval(() => setTick((t) => t + 1), 500)
    return () => window.clearInterval(iv)
  }, [])

  const run = useCallback(async (what: string, fn: () => Promise<unknown>) => {
    setErr(''); setBusy(what)
    try { await fn() } catch (e) { setErr(e instanceof Error ? e.message : `${what} 실패`) } finally { setBusy('') }
  }, [])

  const patch = useCallback((oid: string, part: Partial<Obj>) => {
    setSpec((s) => s && ({ ...s, objects: s.objects.map((o) => (o.id === oid ? { ...o, ...part } : o)) }))
    setDirty(true)
  }, [])

  const addObject = useCallback((shape: string) => {
    if (!spec || !defs) return
    const base = shape.replace('preset:', '')
    let n = 1
    while (spec.objects.some((o) => o.id === `${base}${n}`)) n += 1
    const preset = shape.startsWith('preset:')
    const phys = preset ? defs.static_physics : defs.movable_physics
    const o: Obj = {
      id: `${base}${n}`, label: `${SHAPE_KO[shape] ?? shape} ${n}`, shape,
      movable: !preset, pos: [0.3, 0, 0], euler_deg: [0, 0, 0], rgba: [0.7, 0.7, 0.72, 1],
      friction: phys.friction, condim: phys.condim, solref: phys.solref,
      ...(preset
        ? { params: { ...defs.presets[base].params } }
        : { size: Array(defs.primitives[shape].size_len).fill(0.02), mass: 0.05 }),
    }
    // 새 물체는 테이블에 앉혀 둔다 — 허공에 두면 적용하자마자 떨어진다
    o.pos = [0.3, 0, preset ? 0 : (o.size ? restZ(shape, o.size) : 0)]
    setSpec({ ...spec, objects: [...spec.objects, o] })
    setSel(o.id); setDirty(true)
  }, [spec, defs])

  const addMesh = useCallback((a: Asset) => {
    if (!spec) return
    let n = 1
    while (spec.objects.some((o) => o.id === `mesh${n}`)) n += 1
    const phys = defs?.movable_physics ?? { friction: [2, 0.1, 0.001], condim: 6, solref: [0.005, 1] }
    // 메시는 올릴 때 AABB 아래면 가운데를 원점으로 옮겨 뒀다 — pos.z 0 이면 테이블에 앉는다
    const o: Obj = {
      id: `mesh${n}`, label: a.name, shape: 'mesh', asset: a.id, scale: 1,
      movable: true, pos: [0.3, 0, 0], euler_deg: [0, 0, 0], rgba: [0.75, 0.72, 0.68, 1],
      mass: 0.1, friction: phys.friction, condim: phys.condim, solref: phys.solref,
    }
    setSpec({ ...spec, objects: [...spec.objects, o] })
    setSel(o.id); setDirty(true)
  }, [spec, defs])

  const uploadMesh = useCallback((f: File) => run('자산 올리기', async () => {
    // ⚠ raw 바디다 — multipart 를 쓰면 `python-multipart` 의존성이 붙는다(저장소 관례).
    const res = await fetch(`/api/sim/assets?filename=${encodeURIComponent(f.name)}`
      + `&name=${encodeURIComponent(f.name.replace(/\.[^.]+$/, ''))}`, { method: 'POST', body: f })
    if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail ?? `올리기 실패 (${res.status})`)
    await loadAssets()
  }), [run, loadAssets])

  const setUnit = useCallback((a: Asset, scale: number) => run('단위', async () => {
    await api.put(`/sim/assets/${a.id}/scale`, { unit_scale: scale })
    await loadAssets()
  }), [run, loadAssets])

  const dropAsset = useCallback((a: Asset) => run('자산 지우기', async () => {
    await api.delete(`/sim/assets/${a.id}`)
    await loadAssets()
  }), [run, loadAssets])

  const save = useCallback(() => run('저장', async () => {
    if (!spec) return
    await api.put(`/sim/scenes/${sid}`, { spec })
    setDirty(false); await reload()
  }), [run, spec, sid, reload])

  const apply = useCallback(() => run('적용', async () => {
    if (dirty) await api.put(`/sim/scenes/${sid}`, { spec })
    await api.post(`/sim/scenes/${sid}/apply`, undefined, { timeoutMs: 30_000 })
    setDirty(false); await reload()
  }), [run, dirty, sid, spec, reload])

  const newScene = useCallback(() => run('새 가상환경', async () => {
    const id = `scene-${Date.now().toString(36)}`
    await api.put(`/sim/scenes/${id}`, { spec: { name: '새 가상환경', objects: [] } })
    await reload(); setSid(id)
  }), [run, reload])

  const duplicate = useCallback(() => run('복제', async () => {
    if (!spec) return
    const id = `${sid}-copy-${Date.now().toString(36).slice(-4)}`
    await api.put(`/sim/scenes/${id}`, { spec: { ...spec, name: `${spec.name} 사본` } })
    await reload(); setSid(id)
  }), [run, spec, sid, reload])

  const remove = useCallback(() => run('지우기', async () => {
    await api.delete(`/sim/scenes/${sid}`)
    const r = await reload(); setSid(r.scenes.find((s) => s.id !== sid)?.id ?? '')
  }), [run, sid, reload])

  // 환경 불러오기 — 파일 하나 (사용자 요청 2026-09-17)
  const importFile = useCallback((f: File) => run('불러오기', async () => {
    const text = await f.text()
    const made = await api.post<{ id: string }>('/sim/scenes/import', { text, name: '' })
    await reload(); setSid(made.id)
  }), [run, reload])

  // 환경 내보내기 — 같은 파일을 그대로 내려받는다
  const exportFile = useCallback(() => run('내보내기', async () => {
    // ⚠ `api.get` 은 무조건 `res.json()` 한다 — 여기는 **파일 내용 그대로**가 필요하다.
    const res = await fetch(`/api/sim/scenes/${sid}/export`)
    if (!res.ok) throw new Error(`내보내기 실패 (${res.status})`)
    const text = await res.text()
    const url = URL.createObjectURL(new Blob([text], { type: 'application/json' }))
    const a = document.createElement('a')
    a.href = url; a.download = `${sid}.json`; a.click()
    URL.revokeObjectURL(url)
  }), [run, sid])

  // 탑뷰 클릭 → 선택한 물체를 그 자리로. 살아 있는 세계를 옮기고, 그 값을 명세에도 적는다.
  const placeAt = useCallback(async (e: React.MouseEvent<HTMLImageElement>) => {
    const o = spec?.objects.find((x) => x.id === sel)
    if (!o || !applied) return
    const img = e.currentTarget, r = img.getBoundingClientRect()
    const ar = (img.naturalWidth || 4) / (img.naturalHeight || 3)
    const er = r.width / r.height
    const dw = er > ar ? r.height * ar : r.width
    const dh = er > ar ? r.height : r.width / ar
    const u = (e.clientX - r.left - (r.width - dw) / 2) / dw
    const v = (e.clientY - r.top - (r.height - dh) / 2) / dh
    if (u < 0 || u > 1 || v < 0 || v > 1) return
    await run('배치', async () => {
      if (o.movable) {
        // 움직이는 물체는 살아 있는 세계에서 바로 옮긴다(자유관절이 있다)
        const hit = await api.post<{ pos: number[] }>('/sim/scenes/live/place-from-view',
          { id: o.id, cam: 'sim:top', u, v, aspect: ar })
        if (hit?.pos) patch(o.id, { pos: hit.pos })
        return
      }
      // ⚠ 고정물(통·트레이)은 자유관절이 없어 **qpos 로 못 움직인다** — 자리가 컴파일에
      //   박혀 있다. 그래서 좌표만 받아 명세에 적고 가상환경을 다시 올린다. 클릭 한 번에
      //   그 두 가지가 다 일어나야 "눌렀는데 안 움직인다"가 안 된다 (사용자 요청 2026-09-17).
      const hit = await api.post<{ point: number[] }>('/sim/scenes/live/point-from-view',
        { id: o.id, cam: 'sim:top', u, v, aspect: ar })
      if (!hit?.point) return
      const moved = { ...spec!, objects: spec!.objects.map((x) =>
        (x.id === o.id ? { ...x, pos: [hit.point[0], hit.point[1], x.pos[2]] } : x)) }
      setSpec(moved)
      await api.put(`/sim/scenes/${sid}`, { spec: moved })
      await api.post(`/sim/scenes/${sid}/apply`, undefined, { timeoutMs: 30_000 })
      setDirty(false)
    })
  }, [spec, sel, applied, sid, run, patch])

  const selected = useMemo(() => spec?.objects.find((o) => o.id === sel) ?? null, [spec, sel])

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-2">
        <h1 className="text-xl font-bold tracking-tight">가상환경</h1>
        <select value={sid} onChange={(e) => setSid(e.target.value)}
          className="rounded bg-neutral-900 border border-neutral-700 px-2 py-1 text-sm">
          {rows.length === 0 && <option value="">가상환경 없음</option>}
          {rows.map((r) => (
            <option key={r.id} value={r.id}>
              {r.name} ({r.count}){r.applied ? ' · 적용 중' : ''}{r.error ? ' · 깨짐' : ''}
              {r.missing_assets?.length ? ` · 자산 ${r.missing_assets.length}개 없음` : ''}
            </option>
          ))}
        </select>
        <button onClick={newScene} className="px-2 py-1 text-sm rounded bg-neutral-800 hover:bg-neutral-700">＋ 새 가상환경</button>
        <button onClick={duplicate} disabled={!spec} className="px-2 py-1 text-sm rounded bg-neutral-800 hover:bg-neutral-700 disabled:opacity-40">복제</button>
        <button onClick={remove} disabled={!spec} className="px-2 py-1 text-sm rounded bg-neutral-800 hover:bg-neutral-700 disabled:opacity-40">지우기</button>
        <span className="flex-1" />
        <button onClick={() => fileRef.current?.click()} className="px-2 py-1 text-sm rounded bg-neutral-800 hover:bg-neutral-700">환경 불러오기</button>
        <input ref={fileRef} type="file" accept="application/json,.json" className="hidden"
          onChange={(e) => { const f = e.target.files?.[0]; if (f) importFile(f); e.target.value = '' }} />
        <button onClick={exportFile} disabled={!spec} className="px-2 py-1 text-sm rounded bg-neutral-800 hover:bg-neutral-700 disabled:opacity-40">내보내기</button>
        <button onClick={save} disabled={!spec || !dirty}
          className="px-3 py-1 text-sm rounded bg-blue-700 hover:bg-blue-600 text-white disabled:opacity-40">
          {busy === '저장' ? '저장 중…' : dirty ? '저장' : '저장됨'}
        </button>
        <button onClick={apply} disabled={!spec}
          className="px-3 py-1 text-sm rounded bg-emerald-700 hover:bg-emerald-600 text-white disabled:opacity-40">
          {busy === '적용' ? '올리는 중…' : applied ? '적용 중' : '시뮬에 적용'}
        </button>
      </div>

      {err && <p className="rounded border border-red-700/50 bg-red-900/20 px-3 py-2 text-sm text-red-300">{err}</p>}

      <div className="grid gap-4 lg:grid-cols-[minmax(0,1fr)_minmax(0,1.1fr)]">
        {/* 물체 목록 + 추가 */}
        <div className="space-y-3">
          <div className="rounded border border-neutral-700 p-3 space-y-2">
            <div className="flex items-center gap-2">
              <input value={spec?.name ?? ''} disabled={!spec}
                onChange={(e) => { setSpec((s) => s && ({ ...s, name: e.target.value })); setDirty(true) }}
                className="flex-1 rounded bg-neutral-900 border border-neutral-700 px-2 py-1 text-sm" />
              <span className="text-[11px] text-neutral-500">{spec?.objects.length ?? 0} / {defs?.max_objects ?? '—'}</span>
            </div>
            <div className="flex flex-wrap gap-1">
              {defs && [...Object.keys(defs.primitives), ...Object.keys(defs.presets).map((p) => `preset:${p}`)].map((sh) => (
                <button key={sh} onClick={() => addObject(sh)} disabled={!spec}
                  className="px-2 py-1 text-xs rounded bg-neutral-800 hover:bg-neutral-700 disabled:opacity-40">
                  ＋ {SHAPE_KO[sh] ?? sh}
                </button>
              ))}
            </div>
          </div>

          <ul className="space-y-1">
            {spec?.objects.map((o) => (
              <li key={o.id}>
                <button onClick={() => setSel(o.id)}
                  className={`flex w-full items-center gap-2 rounded border px-2 py-1.5 text-left text-sm ${
                    sel === o.id ? 'border-blue-500 bg-blue-500/10' : 'border-neutral-700 hover:bg-neutral-800'}`}>
                  <span className="h-3.5 w-3.5 shrink-0 rounded-sm border border-black/40" style={{ background: hex(o.rgba) }} />
                  <span className="flex-1 truncate">{o.label}</span>
                  <span className="text-[10px] text-neutral-500">{SHAPE_KO[o.shape] ?? o.shape}</span>
                  {!o.movable && <span className="text-[10px] text-amber-300">고정</span>}
                </button>
              </li>
            ))}
            {spec && spec.objects.length === 0 && (
              <li className="rounded border border-dashed border-neutral-700 px-3 py-6 text-center text-sm text-neutral-500">
                빈 테이블입니다 — 위에서 모양을 골라 올리세요
              </li>
            )}
          </ul>

          {/* 자산(메시) — 사람이 만들거나 스캔한 물건 */}
          <div className="rounded border border-neutral-700 p-3 space-y-2">
            <div className="flex items-center gap-2">
              <h2 className="flex-1 text-sm font-semibold">자산 (메시)</h2>
              <button onClick={() => meshRef.current?.click()}
                className="px-2 py-1 text-xs rounded bg-neutral-800 hover:bg-neutral-700">
                {busy === '자산 올리기' ? '올리는 중…' : '＋ 메시 올리기'}
              </button>
              <input ref={meshRef} type="file" accept=".obj,.stl" className="hidden"
                onChange={(e) => { const f = e.target.files?.[0]; if (f) uploadMesh(f); e.target.value = '' }} />
            </div>
            <p className="text-[11px] text-neutral-500">
              OBJ 또는 <b>바이너리</b> STL. 폰 스캔(Polycam·Scaniverse)·Blender·CAD 에서 내보낸 것.
              GLB·ASCII STL 은 MuJoCo 가 못 읽습니다.
            </p>
            {/* 도움말 — 물체를 어디서 구하나 (사용자 요청 2026-09-17). 화면 안에 둔다:
                올리려다 막힌 자리가 곧 "어디서 구하지?" 가 나오는 자리다. */}
            <details className="rounded border border-neutral-800 bg-neutral-900/40 p-2">
              <summary className="cursor-pointer text-xs text-neutral-300">
                도움말 — 물체는 어디서 구하나
              </summary>
              <div className="mt-2 space-y-3">
                {SOURCES.map((sec) => (
                  <div key={sec.group}>
                    <h3 className="text-[11px] font-semibold text-blue-300">{sec.group}</h3>
                    <p className="mb-1 text-[11px] text-neutral-500">{sec.note}</p>
                    <ul className="space-y-0.5">
                      {sec.items.map(([name, url, why]) => (
                        <li key={url} className="text-[11px]">
                          <a href={url} target="_blank" rel="noreferrer"
                            className="text-blue-400 hover:underline">{name}</a>
                          <span className="text-neutral-500"> — {why}</span>
                        </li>
                      ))}
                    </ul>
                  </div>
                ))}
                <div className="rounded border border-amber-700/40 bg-amber-900/10 p-2 text-[11px] text-amber-200/90">
                  <p><b>그릇·컵·통은 받지 말고 프리셋으로 지으세요.</b> MuJoCo 는 메시를
                    <b> 볼록껍질</b>로 충돌시켜서, 오목한 것을 메시로 올리면 겉만 오목하고
                    물리는 덩어리가 됩니다 — 안에 아무것도 안 들어갑니다.
                    위 [＋ 통 (조립)] 이 그 답입니다.</p>
                  <p className="mt-1 text-amber-200/70">
                    올린 메시가 오목하면 얼마나 파였는지 목록에 적어 드립니다.
                  </p>
                </div>
              </div>
            </details>
            {assets.length === 0 && <p className="text-xs text-neutral-600">아직 없습니다.</p>}
            <ul className="space-y-1.5">
              {assets.map((a) => (
                <li key={a.id} className="rounded border border-neutral-800 p-2 space-y-1">
                  <div className="flex items-center gap-2">
                    <span className="flex-1 truncate text-sm">{a.name}</span>
                    <span className="text-[10px] uppercase text-neutral-500">{a.format}</span>
                    <button onClick={() => addMesh(a)} disabled={!spec}
                      className="px-2 py-0.5 text-xs rounded bg-blue-800 hover:bg-blue-700 text-white disabled:opacity-40">
                      가상환경에 추가
                    </button>
                    <button onClick={() => dropAsset(a)}
                      className="px-2 py-0.5 text-xs rounded bg-neutral-800 hover:bg-neutral-700">지우기</button>
                  </div>
                  <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-[11px] text-neutral-400">
                    <span className="font-mono">
                      {a.bbox_m.map((v) => mm(v)).join(' × ')} mm
                    </span>
                    <label className="flex items-center gap-1">
                      단위
                      <select value={a.unit_scale} onChange={(e) => setUnit(a, Number(e.target.value))}
                        className="rounded bg-neutral-900 border border-neutral-700 px-1 py-0.5">
                        {UNITS.map(([v, l]) => <option key={v} value={v}>{l}</option>)}
                      </select>
                    </label>
                    <span>면 {a.raw.faces.toLocaleString()}</span>
                    {a.raw.concavity > 0.002 && (
                      <span className="text-amber-300" title="MuJoCo 는 메시를 볼록껍질로 충돌시킵니다">
                        ⚠ 오목 {mm(a.raw.concavity)}mm — 충돌은 볼록껍질입니다
                      </span>
                    )}
                    {!a.raw.closed && <span className="text-amber-300" title="스캔은 바닥이 뚫린 채 오기 일쑤입니다">⚠ 열린 메시</span>}
                  </div>
                </li>
              ))}
            </ul>
          </div>

          {selected && (
            <div className="rounded border border-neutral-700 p-3 space-y-3">
              <div className="flex items-center gap-2">
                <input value={selected.label} onChange={(e) => patch(selected.id, { label: e.target.value })}
                  className="flex-1 rounded bg-neutral-900 border border-neutral-700 px-2 py-1 text-sm" />
                <input type="color" value={hex(selected.rgba)}
                  onChange={(e) => patch(selected.id, { rgba: fromHex(e.target.value) })}
                  className="h-7 w-10 rounded border border-neutral-700 bg-neutral-900" />
                <button onClick={() => {
                  setSpec((s) => s && ({ ...s, objects: s.objects.filter((o) => o.id !== selected.id) }))
                  setDirty(true); setSel('')
                }} className="px-2 py-1 text-xs rounded bg-red-900/60 hover:bg-red-800 text-red-100">지우기</button>
              </div>
              <p className="text-[11px] font-mono text-neutral-500">id: {selected.id}</p>

              <div className="grid grid-cols-3 gap-2">
                {(['X', 'Y', 'Z'] as const).map((ax, i) => (
                  <Num key={ax} label={`위치 ${ax} (m)`} value={selected.pos[i]}
                    onChange={(v) => patch(selected.id, { pos: selected.pos.map((p, j) => (j === i ? v : p)) })} />
                ))}
              </div>
              {selected.size && (
                <div className="grid grid-cols-3 gap-2">
                  {selected.size.map((s, i) => (
                    <Num key={i} label={`${SIZE_LABELS[selected.shape]?.[i] ?? `크기 ${i}`} (m)`} value={s} step={0.005}
                      onChange={(v) => patch(selected.id, { size: selected.size!.map((x, j) => (j === i ? v : x)) })} />
                  ))}
                </div>
              )}
              {selected.shape === 'mesh' && (
                <div className="flex items-end gap-3">
                  <div className="flex-1 min-w-0">
                    <p className="text-[10px] text-neutral-500">자산</p>
                    <p className="truncate text-xs">
                      {assets.find((a) => a.id === selected.asset)?.name
                        ?? <span className="text-amber-300">이 기계에 없습니다 ({selected.asset?.slice(0, 8)}…)</span>}
                    </p>
                  </div>
                  <div className="w-24">
                    <Num label="배율" value={selected.scale ?? 1} step={0.1}
                      onChange={(v) => patch(selected.id, { scale: v })} />
                  </div>
                </div>
              )}
              {selected.params && (
                <div className="grid grid-cols-3 gap-2">
                  {Object.entries(selected.params).map(([k, v]) => (
                    Array.isArray(v)
                      ? v.map((n, i) => (
                        <Num key={`${k}${i}`} label={`${k}[${i}] (m)`} value={n}
                          onChange={(nv) => patch(selected.id, {
                            params: { ...selected.params, [k]: v.map((x, j) => (j === i ? nv : x)) },
                          })} />
                      ))
                      : <Num key={k} label={`${k} (m)`} value={v}
                        onChange={(nv) => patch(selected.id, { params: { ...selected.params, [k]: nv } })} />
                  ))}
                </div>
              )}
              <div className="grid grid-cols-3 gap-2">
                {(['X', 'Y', 'Z'] as const).map((ax, i) => (
                  <Num key={ax} label={`회전 ${ax} (°)`} value={selected.euler_deg[i]} step={5}
                    onChange={(v) => patch(selected.id, { euler_deg: selected.euler_deg.map((p, j) => (j === i ? v : p)) })} />
                ))}
              </div>
              <div className="flex items-end gap-3">
                <label className="flex items-center gap-1.5 text-xs text-neutral-300">
                  <input type="checkbox" checked={selected.movable} className="accent-blue-500"
                    onChange={(e) => patch(selected.id, {
                      movable: e.target.checked,
                      ...(defs ? (e.target.checked ? defs.movable_physics : defs.static_physics) : {}),
                      ...(e.target.checked && selected.mass === undefined ? { mass: 0.05 } : {}),
                    })} />
                  움직임 (잡을 수 있다)
                </label>
                {selected.movable && (
                  <div className="w-24">
                    <Num label="질량 (kg)" value={selected.mass ?? 0.05} step={0.01}
                      onChange={(v) => patch(selected.id, { mass: v })} />
                  </div>
                )}
                <div className="w-24">
                  <Num label="마찰" value={selected.friction[0]} step={0.1}
                    onChange={(v) => patch(selected.id, { friction: [v, ...selected.friction.slice(1)] })} />
                </div>
              </div>
              <p className="text-[11px] text-neutral-500">
                ⚠ 크기는 <b>반지름·반변</b>입니다 — 한 변 4cm 상자는 0.02 입니다.
                고정물은 잡을 물건이 아니라 부딪칠 물건이라 접촉 설정이 다릅니다.
              </p>
            </div>
          )}
        </div>

        {/* 배치 화면 — 시뮬 탑뷰 */}
        <div className="space-y-2">
          <div className="relative overflow-hidden rounded border border-neutral-700 bg-black">
            <img src={`/api/cameras/sim%3Atop/preview?t=${tick}`} alt="시뮬 탑뷰"
              onClick={placeAt} draggable={false}
              className={`w-full object-contain ${applied && selected ? 'cursor-crosshair' : ''}`} />
            {!applied && (
              <div className="absolute inset-0 flex items-center justify-center bg-black/65 px-6 text-center text-sm text-neutral-200">
                {dirty ? '고친 내용을 적용해야 여기서 배치할 수 있습니다'
                  : '이 가상환경을 시뮬에 적용하면 여기서 클릭해 배치할 수 있습니다'}
              </div>
            )}
          </div>
          <p className="text-[11px] text-neutral-500">
            {applied
              ? (selected
                ? `탑뷰를 클릭하면 '${selected.label}' 가 그 자리로 갑니다`
                  + (selected.movable ? ' — 그 위치가 가상환경에도 적힙니다.'
                    : ' — 고정물이라 가상환경을 고쳐 다시 올립니다(잠깐 멈춥니다).')
                : '물체를 고르면 클릭으로 배치할 수 있습니다.')
              : '탑뷰는 지금 시뮬에 올라간 세계입니다.'}
          </p>
        </div>
      </div>
    </div>
  )
}

/** 테이블에 앉혔을 때의 z — 백엔드 `scene_spec.rest_z` 와 같은 규칙(모양별 반높이). */
function restZ(shape: string, size: number[]): number {
  if (shape === 'sphere') return size[0]
  if (shape === 'cylinder') return size[1]
  if (shape === 'capsule') return size[1] + size[0]
  return size[2] ?? 0
}
// 메시는 0 이다 — 올릴 때 AABB 아래면 가운데를 원점으로 옮겨 두므로(백엔드 assets.add)
// pos.z 0 이면 테이블에 앉는다. 실측: body z 가 -0.0002 로 안착한다.
