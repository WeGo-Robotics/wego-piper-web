import { useCallback, useEffect, useState } from 'react'
import { api } from '../services/api'

/**
 * 무엇이 실행 중이고 무엇이 막혀 있는가 — 백엔드의 단일 규칙을 그대로 읽는다.
 *
 * 이전에는 각 페이지가 `canStart`에 교차 조건을 손으로 적었고 전부 달랐다
 * (RecordingPage는 학습·추론을 아예 안 봤다). 규칙은 백엔드
 * `services/exclusivity.py` 한 곳에만 둔다.
 *
 * WS 소켓을 새로 열지 않는다 — 페이지가 이미 하나 열고 있으므로, 상태 메시지가
 * 오면 페이지가 `refresh()`를 부르는 방식으로 붙인다.
 */

export type ActivityName =
  | 'inference'
  | 'recording'
  | 'training'
  | 'policy_server'
  | 'dataset_edit'
  | 'upload'

type ActivitySnapshot = {
  running: ActivityName[]
  blocked: Record<ActivityName, ActivityName[]>
  labels: Record<string, string>
}

const EMPTY: ActivitySnapshot = {
  running: [],
  blocked: {} as Record<ActivityName, ActivityName[]>,
  labels: {},
}

export function useActivity() {
  const [snapshot, setSnapshot] = useState<ActivitySnapshot>(EMPTY)

  const refresh = useCallback(() => {
    api.get<ActivitySnapshot>('/activity').then(setSnapshot).catch(() => {})
  }, [])

  useEffect(() => { refresh() }, [refresh])

  /** `target`을 막고 있는 것이 있는가. 자기 자신이 실행 중인 경우도 포함된다. */
  const isBlocked = useCallback(
    (target: ActivityName) => (snapshot.blocked[target]?.length ?? 0) > 0,
    [snapshot],
  )

  /** 막는 사유를 한글로. 없으면 빈 문자열. 자기 자신은 제외한다(버튼 옆 표시용). */
  const blockedBy = useCallback(
    (target: ActivityName) => {
      const blockers = (snapshot.blocked[target] ?? []).filter((a) => a !== target)
      return blockers.map((a) => snapshot.labels[a] ?? a).join(' · ')
    },
    [snapshot],
  )

  return { running: snapshot.running, isBlocked, blockedBy, refresh }
}

// `isStateMessage` 는 WS 계약의 일부이므로 types/ws.ts 에 있다.
// 페이지들이 이 훅과 함께 쓰므로 여기서 다시 내보낸다.
export { isStateMessage } from '../types/ws'
