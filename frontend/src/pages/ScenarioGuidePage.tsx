import RecordGuide, { useScenarioProgress } from '../components/scenario/RecordGuide'
import { useT } from '../i18n'

/**
 * 안내 창 — 별도 브라우저 창 (feature/scenario-designer.md §9-1). 테이블 옆 모니터·태블릿에 띄운다.
 *
 * 그림을 크게 보여 줄 뿐이다. E-stop heartbeat 는 **보내지 않는다** — 녹화를 지키는 것은 수집 화면이고,
 * 이 창이 닫혀도 녹화에는 영향이 없다. 음성도 수집 화면이 낸다(두 창이 같이 읽으면 겹친다).
 */
export default function ScenarioGuidePage() {
  const t = useT()
  const data = useScenarioProgress(true)
  return (
    <div className="min-h-screen bg-neutral-900 text-neutral-100 p-4">
      {data?.active && data.recording ? (
        <RecordGuide data={data} big />
      ) : (
        <p className="text-neutral-400">
          {t('시나리오 계획을 따르는 녹화가 돌고 있지 않습니다 — 수집 화면에서 [시나리오 계획 따르기]로 녹화를 시작하세요')}
        </p>
      )}
    </div>
  )
}
