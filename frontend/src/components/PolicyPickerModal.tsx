import type { ReactNode } from 'react'
import type { PolicyInfo } from '../hooks/usePolicies'
import { useT, fmt } from '../i18n'

/**
 * 학습할 정책 고르는 창 — 정책마다 설명과, 고르기 전에 알아야 할 사실을 나란히 보여준다.
 *
 * select 한 줄에는 이름(`ACT-S`)밖에 없어서 무엇이 다른지, 어떤 데이터셋이 필요한지, 무엇에서
 * 이어 학습하는지를 고른 뒤에야 알 수 있었다. 내용은 전부 정책 스펙(YAML)에서 온다 — 화면이
 * 따로 적으면 정책을 더할 때 갈라진다.
 *
 * ⚠ **React 창이다** — `window.confirm` 류는 E-stop heartbeat 를 막는다.
 * ⚠ **Esc 로 닫지 않는다** — Esc 는 전역 E-stop 이다(EStopButton). ✕ 와 바깥 클릭으로 닫는다.
 */
export default function PolicyPickerModal({ policies, value, onPick, onClose }: {
  policies: PolicyInfo[]
  value: string
  onPick: (type: string) => void
  onClose: () => void
}) {
  const t = useT()
  const labelOf = (type: string) => policies.find((p) => p.type === type)?.label ?? type
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4" onClick={onClose}>
      <div className="flex max-h-[88vh] w-full max-w-4xl flex-col rounded-lg border border-neutral-700 bg-neutral-900"
        onClick={(e) => e.stopPropagation()}>
        <div className="flex items-center gap-3 border-b border-neutral-800 px-4 py-3">
          <h3 className="text-sm font-semibold text-neutral-100">{t('정책 선택')}</h3>
          <button type="button" onClick={onClose} title={t('닫기')}
            className="ml-auto rounded px-2 py-0.5 text-neutral-400 hover:bg-neutral-800 hover:text-white">✕</button>
        </div>
        <div className="grid min-h-0 grid-cols-1 gap-3 overflow-y-auto p-4 md:grid-cols-2">
          {policies.map((p) => (
            <div key={p.type} role="button" tabIndex={0}
              onClick={() => { onPick(p.type); onClose() }}
              onKeyDown={(e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); onPick(p.type); onClose() } }}
              className={`cursor-pointer space-y-2 rounded-lg border p-3 text-xs transition-colors focus:outline-none
                ${p.type === value ? 'border-blue-500 bg-blue-500/10 ring-1 ring-blue-500'
                  : 'border-neutral-700 bg-neutral-800/60 hover:border-neutral-500 focus:border-blue-500'}`}>
              <div className="flex items-baseline gap-2">
                <span className="text-base font-semibold text-neutral-100">{p.label}</span>
                <code className="text-[10px] text-neutral-500">{p.type}</code>
                {p.type === value && <span className="ml-auto text-[10px] text-blue-300">{t('지금 선택')}</span>}
              </div>
              {/* 정책 스펙의 문장 — 처음부터 안내·경고처럼 원문 그대로 보인다 */}
              {p.description && <p className="leading-relaxed text-neutral-300">{p.description}</p>}
              <div className="flex flex-wrap gap-1">
                <span className={`rounded px-1.5 py-0.5 ${p.language ? 'bg-sky-500/15 text-sky-300' : 'bg-neutral-700/60 text-neutral-400'}`}>
                  {p.language ? t('작업 문장(task) 받음') : t('작업 문장 안 받음')}
                </span>
                {p.rtc && <span className="rounded bg-violet-500/15 px-1.5 py-0.5 text-violet-300">{t('추론 때 RTC')}</span>}
                {p.requires_features.length > 0 && (
                  <span className="rounded bg-amber-500/15 px-1.5 py-0.5 text-amber-300"
                    title={p.requires_features.join(', ')}>{t('구운 데이터셋 필요')}</span>
                )}
              </div>
              <dl className="space-y-0.5 text-[11px]">
                {p.policy_base && <Row k={t('권장 시작점')} v={<code>{p.policy_base}</code>} />}
                {p.warm_start_from.length > 0 && (
                  <Row k={t('이어 받기')} v={t('{from} 체크포인트에서 이어 학습할 수 있습니다', { from: p.warm_start_from.map(labelOf).join(', ') })} />
                )}
                {p.scratch_note && <Row k={t('처음부터')} v={p.scratch_note} />}
                {(p.defaults.batch_size || p.defaults.steps) && (
                  <Row k={t('기본값')} v={[
                    p.defaults.batch_size && t('배치 {n}', { n: p.defaults.batch_size }),
                    p.defaults.steps && t('{step} 스텝', { step: fmt.number(p.defaults.steps) }),
                  ].filter(Boolean).join(' · ')} />
                )}
              </dl>
            </div>
          ))}
        </div>
      </div>
    </div>
  )
}

function Row({ k, v }: { k: string; v: ReactNode }) {
  return (
    <div className="flex gap-2">
      <dt className="w-16 shrink-0 text-neutral-500">{k}</dt>
      <dd className="min-w-0 text-neutral-300">{v}</dd>
    </div>
  )
}
