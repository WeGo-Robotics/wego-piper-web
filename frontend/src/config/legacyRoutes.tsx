import { Navigate, useLocation } from 'react-router-dom'

/**
 * 옛 경로 → 새 경로. 검출 화면이 `/yolo-demo`·`/yolo-train` 에서 `/detector`·
 * `/detector-train` 으로 옮겨 갔다 (feature/plugins.md §14-1). 북마크와 학습 완료
 * 알림의 `/yolo-demo?model=` 단축 링크가 아직 그 이름을 쥐고 있다.
 *
 * ⚠ **삭제 예정 — 한 릴리스 뒤.** 이 파일만이 저장소에서 옛 이름을 적을 수 있다
 * (`test_no_model_branding.py` 의 허용 목록). 다른 곳에 옛 이름이 새로 생기면 테스트가 막는다.
 */
export const legacyRoutes: { from: string; to: string }[] = [
  { from: '/yolo-demo', to: '/detector' },
  { from: '/yolo-train', to: '/detector-train' },
]

/** 쿼리(`?model=`)를 그대로 들고 새 경로로 보낸다. */
export function LegacyRedirect({ to }: { to: string }) {
  const { search, hash } = useLocation()
  return <Navigate to={{ pathname: to, search, hash }} replace />
}
