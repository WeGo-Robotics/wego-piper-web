import { useT } from '../i18n'

/** 지연 로드되는 페이지가 올라오는 동안 — Layout 밖 전체화면 페이지(조종 창)용. */
export default function PageLoading() {
  const t = useT()
  return (
    <div className="min-h-screen bg-neutral-900 text-neutral-100 flex items-center justify-center text-sm text-neutral-400">
      {t('로딩 중...')}
    </div>
  )
}
