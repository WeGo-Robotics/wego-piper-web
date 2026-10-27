import { runName, type Model } from '../types/models'
import { useT, fmt } from '../i18n'

/**
 * 고른 체크포인트 한 개 — 이름 한 줄 + 경로·스텝·데이터셋·정책 한 줄. 추론의 체크포인트와
 * 학습의 Fine-tune 칸이 같이 쓴다(둘 다 select 대신 이것과 [선택하기]).
 *
 * 이름은 저장소 표와 같은 규칙이다 — 학습 폼 제목, 없으면 그 학습의 체크포인트에 붙인 이름.
 * 그래서 이름 없는 45K 를 골라도 50K 에 붙인 "볼트 분류" 로 보인다.
 */
export default function CheckpointLabel({ model: m, models }: { model: Model; models: Model[] }) {
  const t = useT()
  const name = m.run ? runName(models.filter((x) => x.run === m.run)) : (m.notes?.name ?? '')
  const step = m.checkpoint === 'last' ? t('last (마지막)')
    : m.step != null ? t('{step} 스텝', { step: fmt.number(m.step) }) : ''
  const sub = [name && (m.run ?? m.id), step, m.train_cameras?.dataset, m.policy_type].filter(Boolean).join(' · ')
  return (
    <div className="min-w-0 flex-1">
      <p className="truncate text-sm font-medium text-neutral-100">{name || m.run || m.id}</p>
      {/* 카드가 좁아 잘린다 — 올리면 전부 */}
      <p className="truncate text-[11px] text-neutral-400" title={sub}>{sub}</p>
    </div>
  )
}
