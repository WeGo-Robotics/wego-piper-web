import { languageName, useLanguage, useT } from '../i18n'

/**
 * 표시 언어 고르기. 후보는 `i18n/locales/*.json` 에서 나온다 — 언어를 더해도 여기는
 * 안 고친다.
 *
 * 이름은 **그 언어의 자기 이름**이다(日本語). 한국어를 못 읽는 사람이 이 칸을 찾아야
 * 하므로 라벨만 번역하고 이름은 번역하지 않는다.
 *
 * ⚠ 바꿔도 화면을 새로 띄우지 않는다 — 새로고침은 WS 를 끊고 E-stop heartbeat 를
 *   놓치게 한다 (`i18n/index.ts`).
 */
export default function LanguageSwitcher() {
  const t = useT()
  const { lang, languages, setLanguage } = useLanguage()
  if (languages.length < 2) return null

  return (
    <select
      value={lang}
      onChange={(e) => { setLanguage(e.target.value).catch((err) => console.error('[i18n]', err)) }}
      aria-label={t('표시 언어')}
      title={t('표시 언어')}
      className="shrink-0 rounded border border-neutral-700 bg-neutral-900 px-2 py-1 text-xs
        text-neutral-300 hover:border-neutral-500 focus:border-blue-500 focus:outline-none"
    >
      {languages.map((c) => (
        <option key={c} value={c}>{languageName(c)}</option>
      ))}
    </select>
  )
}
