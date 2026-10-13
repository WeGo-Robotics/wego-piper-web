import { useEffect, useState } from 'react'
import { api } from '../services/api'
import type { DiskUsage } from '../types/models'
import { useT } from '../i18n'

function formatBytes(bytes: number): string {
  if (bytes === 0) return '0 B'
  const k = 1024
  const sizes = ['B', 'KB', 'MB', 'GB', 'TB']
  const i = Math.floor(Math.log(bytes) / Math.log(k))
  return `${(bytes / Math.pow(k, i)).toFixed(1)} ${sizes[i]}`
}

export default function DiskUsageBar({ compact = false }: { compact?: boolean }) {
  const t = useT()
  const [usage, setUsage] = useState<DiskUsage | null>(null)

  useEffect(() => {
    api.get<DiskUsage>('/datasets/disk-usage').then(setUsage).catch(() => {})
  }, [])

  if (!usage) return null

  // ⚠ **한 줄 모드.** 저장소 화면은 제목·탭·출처·용량을 한 줄에 둔다 — 세 줄이
  //   쌓이면 정작 봐야 할 목록이 그만큼 아래로 밀린다. 여기서는 막대를 좁히고
  //   숫자만 남긴다. 경고 색은 그대로다 — 좁다고 경고를 지우면 안 된다.
  if (compact) {
    return (
      <div className={`flex items-center gap-2 text-xs ${
        usage.warning ? 'text-amber-300' : 'text-neutral-400'}`}>
        <div className="h-1.5 w-16 overflow-hidden rounded-full bg-neutral-700">
          <div className={`h-full rounded-full ${usage.warning ? 'bg-amber-500' : 'bg-blue-500'}`}
            style={{ width: `${Math.min(100, (usage.total_gb / usage.threshold_gb) * 100)}%` }} />
        </div>
        <span title={t('{total_gb} GB / 임계치 {threshold_gb} GB', { total_gb: usage.total_gb, threshold_gb: usage.threshold_gb })}>
          {usage.total_gb}/{usage.threshold_gb} GB
        </span>
        <span className="text-neutral-500">
          {t('모델 {models_bytes} · 데이터셋 {datasets_bytes}', {
            models_bytes: formatBytes(usage.models_bytes),
            datasets_bytes: formatBytes(usage.datasets_bytes),
          })}
        </span>
      </div>
    )
  }

  return (
    <div
      className={`rounded-lg border p-3 text-sm ${
        usage.warning
          ? 'border-amber-500/50 bg-amber-500/10 text-amber-300'
          : 'border-neutral-700 bg-neutral-800 text-neutral-400'
      }`}
    >
      <div className="flex justify-between mb-1">
        <span>{t('디스크 사용량')}</span>
        <span>
          {usage.total_gb} GB / {usage.threshold_gb} GB
        </span>
      </div>
      <div className="h-1.5 bg-neutral-700 rounded-full overflow-hidden">
        <div
          className={`h-full rounded-full transition-all ${
            usage.warning ? 'bg-amber-500' : 'bg-blue-500'
          }`}
          style={{
            width: `${Math.min(100, (usage.total_gb / usage.threshold_gb) * 100)}%`,
          }}
        />
      </div>
      <div className="flex gap-4 mt-1 text-xs">
        <span>{t('모델: {models_bytes}', { models_bytes: formatBytes(usage.models_bytes) })}</span>
        <span>{t('데이터셋: {datasets_bytes}', { datasets_bytes: formatBytes(usage.datasets_bytes) })}</span>
      </div>
    </div>
  )
}
