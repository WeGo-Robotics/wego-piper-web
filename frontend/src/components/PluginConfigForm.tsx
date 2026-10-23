import { useEffect, useState } from 'react'
import { api } from '../services/api'
import { useT } from '../i18n'
import SpecFields from './SpecFields'
import type { SpecField } from '../hooks/usePolicyUi'
import CameraStreamSelect from './CameraStreamPicker'
import { useCameraStreams } from '../hooks/useCameraStreams'
import type { Model, Dataset } from '../types/models'
import type { PluginCameraSlot, PluginConfigField, PluginConfigValues, PluginInputSlot, PluginSources } from '../types/plugins'

/**
 * 매니페스트 `cameras` + `config` 를 폼으로 (feature/plugins.md §4, §8-1).
 *
 * 숫자·불은 정책 스펙과 같은 `SpecFields` 가 그린다 — 플러그인 폼이 자기 입력칸을 따로
 * 들 이유가 없다. 여기서 더하는 것은 `text`·`select` 와, **기존 선택기**로 채우는 셋
 * (`model`→체크포인트 목록, `dataset`→데이터셋 목록, `camera`→스트림 선택기)이다.
 * 레이아웃·색은 여기가 정하고 매니페스트는 "어떤 항목이 있고 범위는 얼마인가"만 말한다.
 */
type Props = {
  fields: PluginConfigField[]
  cameraSlots: PluginCameraSlot[]
  inputSlots?: PluginInputSlot[]
  values: PluginConfigValues
  cameras: Record<string, string>
  inputs?: Record<string, string>
  onChange: (key: string, value: number | boolean | string) => void
  onCameraChange: (slot: string, stream: string) => void
  onInputChange?: (slot: string, source: string) => void
  disabled?: boolean
}

export default function PluginConfigForm({ fields, cameraSlots, inputSlots = [], values, cameras, inputs = {}, onChange, onCameraChange, onInputChange, disabled }: Props) {
  const t = useT()
  const { streams } = useCameraStreams()
  const [sources, setSources] = useState<PluginSources | null>(null)
  useEffect(() => {
    if (inputSlots.length === 0) return
    const load = () => api.get<PluginSources>('/plugins/topics').then(setSources).catch(() => {})
    load()
    const id = setInterval(load, 3000)
    return () => clearInterval(id)
  }, [inputSlots.length])
  const [models, setModels] = useState<Model[]>([])
  const [datasets, setDatasets] = useState<Dataset[]>([])
  const needModels = fields.some((f) => f.kind === 'model')
  const needDatasets = fields.some((f) => f.kind === 'dataset')
  useEffect(() => {
    if (needModels) api.get<Model[]>('/models').then(setModels).catch(() => {})
  }, [needModels])
  useEffect(() => {
    if (needDatasets) api.get<Dataset[]>('/datasets').then(setDatasets).catch(() => {})
  }, [needDatasets])

  const numeric: SpecField[] = fields
    .filter((f) => f.kind === 'number' || f.kind === 'bool')
    .map((f) => ({
      key: f.key, label: f.label || f.key, kind: f.kind as 'number' | 'bool',
      default: (f.default ?? (f.kind === 'bool' ? false : 0)) as number | boolean,
      min: f.min ?? null, max: f.max ?? null, step: f.step ?? null, arch: false, override_reason: '',
    }))
  const others = fields.filter((f) => f.kind !== 'number' && f.kind !== 'bool')
  const takenBy = (stream: string) => Object.entries(cameras).find(([k, v]) => v === stream && k)?.[0] ?? ''
  const input = 'w-full px-2 py-1 rounded bg-neutral-900 border border-neutral-700 text-sm text-neutral-100 disabled:opacity-60'

  return (
    <fieldset disabled={disabled} className="space-y-3">
      {cameraSlots.length > 0 && (
        <div className="grid grid-cols-2 gap-2">
          {cameraSlots.map((slot) => (
            <div key={slot.key} className="space-y-0.5">
              <label className="text-xs text-neutral-400">
                {slot.label || slot.key}{slot.required === false ? '' : ' *'}
              </label>
              <CameraStreamSelect value={cameras[slot.key] ?? ''} onChange={(v) => onCameraChange(slot.key, v)}
                streams={streams} takenBy={takenBy} className={input} placeholder={t('— 카메라 선택 —')} />
            </div>
          ))}
        </div>
      )}
      {inputSlots.length > 0 && (
        <div className="grid grid-cols-2 gap-2">
          {inputSlots.map((slot) => {
            const pool = slot.kind === 'topic' ? sources?.topics ?? {} : sources?.detections ?? {}
            const cur = inputs[slot.key] ?? slot.source ?? ''
            const names = Array.from(new Set([...Object.keys(pool), ...(cur ? [cur] : [])]))
            return (
              <div key={slot.key} className="space-y-0.5">
                <label className="text-xs text-neutral-400">
                  {slot.label || slot.key}{slot.required === false ? '' : ' *'}
                  <span className="ml-1 text-neutral-600 font-mono">{slot.kind === 'topic' ? t('토픽') : t('검출')}</span>
                </label>
                <select value={cur} onChange={(e) => onInputChange?.(slot.key, e.target.value)} className={input}>
                  <option value="">{t('— 입력 소스 선택 —')}</option>
                  {names.map((n) => {
                    const meta = pool[n]
                    return <option key={n} value={n}>{n}{meta ? ` · ${meta.publisher ?? '?'}` : ` · ${t('지금 없음')}`}</option>
                  })}
                </select>
              </div>
            )
          })}
        </div>
      )}
      {others.length > 0 && (
        <div className="grid grid-cols-2 gap-2">
          {others.map((f) => {
            const v = values[f.key] ?? f.default ?? ''
            const label = <label className="text-xs text-neutral-400">{f.label || f.key}</label>
            if (f.kind === 'select') return (
              <div key={f.key} className="space-y-0.5">{label}
                <select value={String(v)} onChange={(e) => onChange(f.key, e.target.value)} className={input}>
                  {(f.options ?? []).map((o) => <option key={o} value={o}>{o}</option>)}
                </select>
              </div>)
            if (f.kind === 'model') return (
              <div key={f.key} className="space-y-0.5">{label}
                <select value={String(v)} onChange={(e) => onChange(f.key, e.target.value)} className={input}>
                  <option value="">{t('— 체크포인트 선택 —')}</option>
                  {models.map((m) => <option key={m.id} value={m.path}>{m.id}</option>)}
                </select>
              </div>)
            if (f.kind === 'dataset') return (
              <div key={f.key} className="space-y-0.5">{label}
                <select value={String(v)} onChange={(e) => onChange(f.key, e.target.value)} className={input}>
                  <option value="">{t('— 데이터셋 선택 —')}</option>
                  {datasets.map((d) => <option key={d.id} value={d.id}>{d.id}</option>)}
                </select>
              </div>)
            if (f.kind === 'camera') return (
              <div key={f.key} className="space-y-0.5">{label}
                <CameraStreamSelect value={String(v)} onChange={(s) => onChange(f.key, s)} streams={streams}
                  takenBy={() => ''} className={input} placeholder={t('— 카메라 선택 —')} />
              </div>)
            if (f.multiline) return (
              <div key={f.key} className="space-y-0.5 col-span-2">{label}
                <textarea value={String(v)} rows={4} onChange={(e) => onChange(f.key, e.target.value)}
                  className={`${input} font-mono text-xs`} />
              </div>)
            return (
              <div key={f.key} className="space-y-0.5">{label}
                <input type="text" value={String(v)} onChange={(e) => onChange(f.key, e.target.value)} className={input} />
              </div>)
          })}
        </div>
      )}
      <SpecFields fields={numeric} values={values as Record<string, number | boolean>} onChange={onChange} />
    </fieldset>
  )
}
