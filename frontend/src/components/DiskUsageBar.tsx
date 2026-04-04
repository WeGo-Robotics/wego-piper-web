import { useEffect, useState } from 'react'
import { api } from '../services/api'
import type { DiskUsage } from '../types/models'

function formatBytes(bytes: number): string {
  if (bytes === 0) return '0 B'
  const k = 1024
  const sizes = ['B', 'KB', 'MB', 'GB', 'TB']
  const i = Math.floor(Math.log(bytes) / Math.log(k))
  return `${(bytes / Math.pow(k, i)).toFixed(1)} ${sizes[i]}`
}

export default function DiskUsageBar() {
  const [usage, setUsage] = useState<DiskUsage | null>(null)

  useEffect(() => {
    api.get<DiskUsage>('/datasets/disk-usage').then(setUsage).catch(() => {})
  }, [])

  if (!usage) return null

  return (
    <div
      className={`rounded-lg border p-3 text-sm ${
        usage.warning
          ? 'border-amber-500/50 bg-amber-500/10 text-amber-300'
          : 'border-neutral-700 bg-neutral-800 text-neutral-400'
      }`}
    >
      <div className="flex justify-between mb-1">
        <span>디스크 사용량</span>
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
        <span>모델: {formatBytes(usage.models_bytes)}</span>
        <span>데이터셋: {formatBytes(usage.datasets_bytes)}</span>
      </div>
    </div>
  )
}
