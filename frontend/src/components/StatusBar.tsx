import { useCallback, useEffect, useState } from 'react'
import { useActivity, isStateMessage } from '../hooks/useActivity'
import type { ActivityName } from '../hooks/useActivity'
import { useDeviceSummary } from '../hooks/useDeviceSummary'
import type { DeviceCount } from '../hooks/useDeviceSummary'
import { useWebSocket } from '../hooks/useWebSocket'
import { api } from '../services/api'
import type { DiskUsage } from '../types/models'

/**
 * 상단 상태바 — **어느 페이지에 있든** 무엇이 도는지 보인다
 * (feature/layout-redesign.md §4).
 *
 * ## 왜 필요했나
 *
 * 학습이 도는지 알려면 학습 페이지로 가야 했다. 같은 뿌리의 문제가 이미 두 번
 * 터졌다 — 수집 페이지에서 카메라 경보가 안 보였고, 경보가 뜬 채로 카메라
 * 페이지에 가니 뽑힌 카메라가 정상처럼 보였다. `SystemMessages` 가 **사건**을
 * 맡았으니 상태바가 **상태**를 맡는다.
 *
 * ## 두 가지를 지킨다
 *
 * 1. **판정하지 않는다.** 활동 이름은 백엔드 `LABELS` 를 그대로 쓴다
 *    (`labelOf`). 화면이 문구를 조립하면 한쪽만 고쳐져 어긋난다.
 * 2. **읽기 전용이다.** 정지·시작 버튼을 두지 않는다 — 좁은 줄의 작은 버튼은
 *    오조작이 나고, 정작 눌러야 하는 순간에는 큰 버튼(E-stop)이 필요하다.
 *
 * ⚠ 1단계는 **이미 있는 값만** 쓴다. 로봇·카메라 개수는 요약 API 가 붙는
 * 2단계다 — 없는 값을 여기서 세기 시작하면 판정이 화면으로 새어 들어온다.
 */

/** 활동별 한 줄 요약. WS 로 오는 값이라 없을 수도 있다 — 없으면 이름만 뜬다. */
type Detail = Partial<Record<ActivityName, string>>

const DOT: Record<string, string> = {
  inference: 'bg-emerald-400',
  recording: 'bg-red-400',
  training: 'bg-sky-400',
  policy_server: 'bg-violet-400',
  dataset_edit: 'bg-amber-400',
  upload: 'bg-amber-400',
}

/** `🦾 2` — 문제가 있으면 숫자가 경고색이 되고 툴팁이 이유를 말한다. */
function DeviceChip({ icon, name, count }: {
  icon: string; name: string; count: DeviceCount
}) {
  const warn = count.warn > 0
  return (
    <span className={`flex shrink-0 items-center gap-1 text-xs tabular-nums ${
      warn ? 'text-amber-400' : 'text-neutral-400'}`}
      title={warn
        ? `${name} ${count.ok}개 사용 가능, ${count.warn}개 없음 — 등록해뒀는데 지금 못 씁니다`
        : `${name} ${count.ok}개 사용 가능`}>
      <span aria-hidden>{icon}</span>
      {count.ok}
      {warn && <span aria-hidden>⚠</span>}
      <span className="sr-only">
        {name} {count.ok}개{warn ? `, ${count.warn}개 없음` : ''}
      </span>
    </span>
  )
}

export default function StatusBar() {
  const { running, labelOf, refresh } = useActivity()
  const [detail, setDetail] = useState<Detail>({})
  const devices = useDeviceSummary()
  const [disk, setDisk] = useState<DiskUsage | null>(null)

  // ⚠ **디스크는 주기 폴링에 안 태운다.** `check_disk_usage()` 는 데이터셋·모델
  //   디렉토리를 통째로 훑는다 — 5초마다 부르면 그 자체가 부하다.
  //   대신 **디스크를 실제로 바꾸는 일이 끝났을 때** 다시 읽는다.
  const refreshDisk = useCallback(() => {
    api.get<DiskUsage>('/datasets/disk-usage').then(setDisk).catch(() => {})
  }, [])
  useEffect(() => { refreshDisk() }, [refreshDisk])

  const { connected } = useWebSocket('/ws', {
    onMessage: (msg) => {
      // 활동이 뜨고 지는 것은 백엔드가 판정한다 — 상태 메시지가 오면 다시 물어본다.
      // **새 WS 타입을 만들지 않는다** (#12 계약 작업과 안 부딪힌다).
      if (isStateMessage(msg.type)) {
        refresh()
        // 녹화·업로드가 끝났으면 디스크가 움직였다
        if ((msg.type === 'record_state' || msg.type === 'upload_state')
            && msg.data === 'idle') {
          refreshDisk()
        }
        return
      }
      if (msg.type === 'telemetry') {
        setDetail((d) => ({ ...d, inference: `${msg.data.fps?.toFixed(1) ?? '–'}fps` }))
      } else if (msg.type === 'train_metrics') {
        const { step, total_steps } = msg.data as { step: number; total_steps?: number }
        setDetail((d) => ({
          ...d,
          training: total_steps ? `${step}/${total_steps}` : `${step} 스텝`,
        }))
      } else if (msg.type === 'record_status') {
        const { current_episode, total_episodes } = msg.data
        setDetail((d) => ({
          ...d,
          recording: total_episodes
            ? `ep ${current_episode}/${total_episodes}`
            : `ep ${current_episode}`,
        }))
      }
    },
  })

  return (
    <header className="h-12 shrink-0 border-b border-neutral-800 bg-neutral-900
                       flex items-center gap-4 px-4">
      <span className="font-bold">Piper</span>

      <div className="flex flex-1 items-center gap-2 overflow-x-auto">
        {running.length === 0 ? (
          <span className="text-sm text-neutral-600">유휴</span>
        ) : (
          running.map((a) => (
            <span key={a}
                  className="flex shrink-0 items-center gap-1.5 rounded-full border
                             border-neutral-700 bg-neutral-800 px-2.5 py-1 text-xs">
              <span className={`h-1.5 w-1.5 rounded-full ${DOT[a] ?? 'bg-neutral-400'}`}
                    aria-hidden />
              <span className="text-neutral-200">{labelOf(a)}</span>
              {detail[a] && <span className="text-neutral-400 tabular-nums">{detail[a]}</span>}
            </span>
          ))
        )}
      </div>

      <div className="flex shrink-0 items-center gap-3">
        <DeviceChip icon="🦾" name="로봇" count={devices.robots} />
        <DeviceChip icon="📷" name="카메라" count={devices.cameras} />
        {disk && (
          <span className={`shrink-0 text-xs tabular-nums ${
            disk.warning ? 'text-amber-400' : 'text-neutral-400'}`}
            title={`데이터셋·모델 ${disk.total_gb}GB / 경고 임계치 ${disk.threshold_gb}GB`}>
            💾 {disk.total_gb}GB{disk.warning && ' ⚠'}
          </span>
        )}
      </div>

      {/* 소켓이 끊기면 위 숫자가 **멈춘 값**이 된다 — 그걸 모르면 안 된다 */}
      <span className="flex shrink-0 items-center gap-1.5 text-xs text-neutral-500"
            title={connected ? '서버 연결됨' : '서버 연결 끊김 — 표시가 멈춰 있습니다'}>
        <span className={`h-1.5 w-1.5 rounded-full ${
          connected ? 'bg-emerald-500' : 'bg-red-500'}`} aria-hidden />
        {connected ? '연결됨' : '끊김'}
      </span>
    </header>
  )
}
