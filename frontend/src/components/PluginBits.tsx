import { useT } from '../i18n'
import { isRunning } from '../types/plugins'

/** 목록·상세가 같이 쓰는 작은 조각들 (feature/plugins.md §8-1). */

export function PluginStateDot({ state, error }: { state: string; error?: boolean }) {
  const t = useT()
  const color = error ? 'bg-red-500' : isRunning(state) ? 'bg-green-500 animate-pulse' : state === 'error' ? 'bg-red-500' : 'bg-neutral-600'
  const title = error ? t('매니페스트 오류') : state
  return <span title={title} className={`mt-1.5 inline-block h-2.5 w-2.5 rounded-full shrink-0 ${color}`} />
}

/** 권한 배지. `*` 는 눈에 띄게 — "모든 API 를 부를 수 있습니다" (§5). */
export function ScopeBadges({ permissions }: { permissions: string[] }) {
  const t = useT()
  if (permissions.length === 0) return <div className="text-[10px] text-neutral-600">{t('REST 권한 없음')}</div>
  return (
    <div className="flex gap-1 flex-wrap">
      {permissions.map((s) => s === '*' ? (
        <span key={s} title={t('모든 API 를 부를 수 있다 (로그인 설정 제외)')}
          className="text-[10px] px-1.5 py-0.5 rounded bg-red-900/70 text-red-200 font-mono">* {t('전부')}</span>
      ) : (
        <span key={s} className="text-[10px] px-1.5 py-0.5 rounded bg-neutral-700 text-neutral-300 font-mono">{s}</span>
      ))}
    </div>
  )
}

/**
 * "UI 보기" — **새 창**으로 연다 (§8-2). 메인 창이 남아 heartbeat·E-stop 이 산다.
 * 매니페스트 `ui` 가 저장소 경로(`/detector`)면 그 라우트를, `ui/`·`server` 면 프록시 경로를.
 * 조종 창(`/teleop`)과 같은 `window.open(..., 'popup')` 이다.
 */
export function openPluginUi(name: string, ui: boolean | string): void {
  if (!ui) return
  const url = typeof ui === 'string' && ui.startsWith('/') ? ui : `/plugins/${name}/ui/`
  window.open(url, `piper-plugin-${name}`, 'popup,width=1280,height=820')
}
