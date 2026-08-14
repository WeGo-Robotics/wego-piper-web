import { useState, useEffect } from 'react'
import { JOINT_NAMES, JOINT_RANGE } from '../config/joints'

export type TelemetryData = {
  step: number
  /** 실제 제어 주기 (슬립 포함) — 팔이 이 속도로 움직인다. */
  fps: number
  /** 한 스텝 처리 시간(ms, 슬립 제외) — 목표 주기 대비 여유. 카메라 대기가 여기 들어간다. */
  step_ms?: number
  inference_ms: number
  joints: number[]
  action: number[]
  task: string
  gpu_mem_mb?: number
  gpu_total_mb?: number
}


type Props = {
  data: TelemetryData | null
  targetFps: number
  cameraNames: string[]
  /** 이 정책이 `task` 를 실제로 쓰는가. wrapper 는 안 쓰는 정책에도 기본값
   *  `"do the task"` 를 텔레메트리에 실어 보내므로, 여기서 안 걸러내면
   *  입력란도 없는 ACT 화면에 유령 Task 가 뜬다. */
  showTask?: boolean
}

function JointBar({ name, value, actionValue }: { name: string; value: number; actionValue?: number }) {
  const [min, max] = JOINT_RANGE[name] ?? [-100, 100]
  const range = max - min
  const pct = ((value - min) / range) * 100
  const actionPct = actionValue !== undefined ? ((actionValue - min) / range) * 100 : undefined

  return (
    <div className="flex items-center gap-2 text-xs">
      <span className="w-14 text-neutral-400 truncate">{name}</span>
      <div className="flex-1 h-3 bg-neutral-800 rounded-full relative overflow-hidden">
        {/* 현재 위치 */}
        <div
          className="absolute h-full bg-blue-500 rounded-full transition-all duration-100"
          style={{ left: `${Math.min(pct, 100)}%`, width: '3px' }}
        />
        {/* 예측 액션 */}
        {actionPct !== undefined && (
          <div
            className="absolute h-full bg-red-400 rounded-full opacity-60"
            style={{ left: `${Math.min(actionPct, 100)}%`, width: '2px' }}
          />
        )}
        {/* 중앙 기준선 */}
        {min < 0 && (
          <div className="absolute h-full w-px bg-neutral-600" style={{ left: '50%' }} />
        )}
      </div>
      <span className="w-14 text-right font-mono text-neutral-300">{value.toFixed(1)}</span>
    </div>
  )
}

export default function TelemetryPanel({ data, targetFps, cameraNames, showTask = true }: Props) {
  const [camTimestamp, setCamTimestamp] = useState(0)

  // 카메라 프리뷰 1초마다 갱신
  useEffect(() => {
    const interval = setInterval(() => setCamTimestamp(Date.now()), 500)
    return () => clearInterval(interval)
  }, [])

  if (!data) {
    return (
      <div className="rounded-lg border border-neutral-700 bg-neutral-800 p-4 text-sm text-neutral-500 text-center">
        추론 대기 중...
      </div>
    )
  }

  const fpsRatio = Math.min(data.fps / targetFps, 1) * 100

  return (
    <div className="rounded-lg border border-neutral-700 bg-neutral-800 p-4 space-y-4">
      {/* 상단: FPS + 지연 + GPU */}
      <div className="flex items-center gap-4 flex-wrap">
        <div className="text-center" title="실제 제어 주기 — 팔이 이 속도로 움직입니다">
          <div className="text-2xl font-bold text-blue-400">{data.fps}</div>
          <div className="text-[10px] text-neutral-400">FPS</div>
        </div>
        {data.step_ms !== undefined && (
          <div
            className="text-center"
            title={`한 스텝 처리에 쓴 시간입니다 (목표 주기 ${Math.round(1000 / targetFps)}ms). `
              + '카메라 대기가 여기 포함됩니다 — 목표에 가까워지면 여유가 없다는 뜻입니다.'}
          >
            <div className={`text-2xl font-bold ${
              data.step_ms > 1000 / targetFps ? 'text-red-400'
                : data.step_ms > 700 / targetFps ? 'text-amber-400' : 'text-neutral-300'
            }`}>{data.step_ms}<span className="text-sm font-normal">ms</span></div>
            <div className="text-[10px] text-neutral-400">처리 시간</div>
          </div>
        )}
        <div className="flex-1 space-y-1">
          <div className="flex justify-between text-[10px] text-neutral-400">
            <span>FPS ({data.fps}/{targetFps})</span>
            <span>{data.inference_ms}ms</span>
          </div>
          <div className="h-1.5 bg-neutral-700 rounded-full overflow-hidden">
            <div
              className={`h-full rounded-full transition-all ${fpsRatio > 80 ? 'bg-green-500' : fpsRatio > 50 ? 'bg-amber-500' : 'bg-red-500'}`}
              style={{ width: `${fpsRatio}%` }}
            />
          </div>
        </div>
        {data.gpu_mem_mb !== undefined && data.gpu_total_mb !== undefined && (
          <div className="text-center">
            <div className="text-sm font-medium text-neutral-200">
              {(data.gpu_mem_mb / 1024).toFixed(1)}<span className="text-neutral-500">/{(data.gpu_total_mb / 1024).toFixed(0)}GB</span>
            </div>
            <div className="text-[10px] text-neutral-400">GPU</div>
          </div>
        )}
        <div className="text-xs text-neutral-400">
          Step {data.step}
        </div>
      </div>

      {/* 관절 위치 + 액션 */}
      <div className="space-y-1">
        <div className="flex justify-between text-[10px] text-neutral-400">
          <span>관절 위치 <span className="text-blue-400">■</span> 현재  <span className="text-red-400">■</span> 예측</span>
        </div>
        {data.joints.map((val, i) => (
          <JointBar
            key={i}
            name={JOINT_NAMES[i] ?? `j${i + 1}`}
            value={val}
            actionValue={data.action[i]}
          />
        ))}
      </div>

      {/* 카메라 프리뷰 */}
      {cameraNames.length > 0 && (
        <div className="space-y-1">
          <span className="text-[10px] text-neutral-400">카메라</span>
          <div className="flex gap-2">
            {cameraNames.map((name) => (
              <div key={name} className="flex-1">
                <img
                  src={`/api/inference/camera/${name}?t=${camTimestamp}`}
                  alt={name}
                  className="rounded border border-neutral-700 w-full aspect-[4/3] object-cover bg-neutral-900"
                  onError={(e) => { (e.target as HTMLImageElement).style.opacity = '0.3' }}
                  onLoad={(e) => { (e.target as HTMLImageElement).style.opacity = '1' }}
                />
                <span className="text-[10px] text-neutral-400">{name}</span>
              </div>
            ))}
          </div>
        </div>
      )}

      {/* Task — 언어를 안 받는 정책엔 아예 안 띄운다.
          판정은 호출부(`takesLanguage(activePolicy)`)가 한다: 입력란을 감추는
          조건과 **같은 값**이어야 "안 보이는데 화면엔 뜨는" 상태가 안 생긴다. */}
      {showTask && data.task && (
        <div className="text-xs text-neutral-400">
          Task: <span className="text-neutral-200">{data.task}</span>
        </div>
      )}
    </div>
  )
}
