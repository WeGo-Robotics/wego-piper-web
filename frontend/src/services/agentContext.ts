import { api } from './api'
import { copyText } from './clipboard'
import type { SystemMessage } from '../components/SystemMessages'

/**
 * 에이전트용 컨텍스트 복사 (feature/agent-cli.md §4-1).
 *
 * 서버 꾸러미(버전·유닛·활동·E-stop·장치·저널·미션 저널·읽을 곳)에 **브라우저 알림함**을 실어
 * 보낸다 — 알림함은 프론트 상태라 서버가 모른다. 서버가 절 0 으로 앉혀 markdown 한 장으로
 * 돌려주면 그대로 클립보드에 넣는다. 돌려받은 글에는 토큰·키가 없다(서버가 지운다).
 *
 * 저널은 호스트의 unitd 왕복이라 평소 GET 보다 오래 걸릴 수 있다 — 시간 제한을 넉넉히 둔다.
 */
export type AgentContextQuery = { since?: string; unit?: string; lines?: number }

export async function copyAgentContext(history: SystemMessage[], q: AgentContextQuery = {}): Promise<number> {
  const text = await api.postText('/system/agent-context', {
    since: q.since || undefined,
    unit: q.unit || undefined,
    lines: q.lines,
    inbox: history.map((m) => ({ at: m.at / 1000, level: m.level, text: m.text, source: m.source ?? '' })),
  }, { timeoutMs: 60_000 })
  await copyText(text)
  return text.length
}
