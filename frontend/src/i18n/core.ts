/**
 * 번역의 순수 로직 — React·DOM·저장소를 모른다 (`index.ts` 가 상태를 든다).
 *
 * ## 키는 한국어 원문이다
 *
 * `t('로봇 등록')` 의 인자가 곧 키다. 번역 파일(`locales/<코드>.json`)은
 * `{ "로봇 등록": "Register robot" }` 모양이고, **번역이 없으면 원문이 그대로 보인다.**
 * 키 이름을 따로 짓지 않으니 코드가 읽히고, 빠진 번역이 빈 화면이 아니라 한국어로
 * 남는다. 원문을 고치면 키도 바뀌므로 `tools/i18n.py check` 가 낡은 번역을 잡는다.
 *
 * ## 값의 모양
 *
 * - 문자열 — `"{n} episodes"`. `{이름}` 이 자리표시자다 (`{{` `}}` 는 중괄호 글자).
 * - 복수형 — `{ "one": "{count} episode", "other": "{count} episodes" }`.
 *   `count` 인자로 `Intl.PluralRules` 가 범주를 고른다. 일본어·중국어는 `other` 하나면 된다.
 * - 빈 문자열 — **번역 안 됨** 이다. 폴백으로 내려간다.
 */

export type Params = Record<string, string | number>
export type Plural = Partial<Record<Intl.LDMLPluralRule, string>>
export type Entry = string | Plural
export type Catalog = Record<string, Entry>

/** `{{` · `}}` · `{이름}`. 이름은 식별자 모양이어야 한다 — JSON 같은 `{"a":1}` 은 건드리지 않는다. */
const PLACEHOLDER = /\{\{|\}\}|\{([A-Za-z_]\w*)\}/g
const HAS_PLACEHOLDER = /\{[A-Za-z_]\w*\}/

export const HANGUL = /[가-힣]/

/**
 * 같은 한국어가 문맥에 따라 다르게 옮겨져야 할 때(`정렬` = Alignment / Sort) 쓰는 키 모양.
 * 번역 파일에는 `"sort::정렬": "Sort"` 로 적고, 코드는 `t('정렬', undefined, 'sort')`.
 * 문맥 항목이 없으면 문맥 없는 항목으로 내려간다.
 */
export const contextKey = (ctx: string, text: string) => `${ctx}::${text}`

export function interpolate(template: string, params: Record<string, unknown>): string {
  return template.replace(PLACEHOLDER, (m, name?: string) => {
    if (m === '{{') return '{'
    if (m === '}}') return '}'
    // 인자가 없으면 자리표시자를 **그대로 둔다** — 빠뜨린 인자가 눈에 띈다
    return name !== undefined && name in params ? String(params[name]) : m
  })
}

/**
 * 템플릿을 `[글자, 값, 글자, …]` 로 쪼갠다 — React 노드를 끼워 넣을 때 쓴다.
 * 짝수 자리는 글자, 홀수 자리는 자리표시자 이름이다.
 */
export function splitTemplate(template: string): string[] {
  const out: string[] = []
  let last = 0
  let text = ''
  for (const m of template.matchAll(PLACEHOLDER)) {
    const at = m.index ?? 0
    text += template.slice(last, at)
    last = at + m[0].length
    if (m[0] === '{{') text += '{'
    else if (m[0] === '}}') text += '}'
    else {
      out.push(text, m[1])
      text = ''
    }
  }
  out.push(text + template.slice(last))
  return out
}

const pluralRules = new Map<string, Intl.PluralRules>()

/** 항목에서 이 개수에 맞는 문장을 고른다. 문자열이면 그대로다. */
export function pick(entry: Entry, count: number | undefined, lang: string): string | undefined {
  if (typeof entry === 'string') return entry
  let rules = pluralRules.get(lang)
  if (!rules) {
    try { rules = new Intl.PluralRules(lang) } catch { rules = new Intl.PluralRules('en') }
    pluralRules.set(lang, rules)
  }
  const cat = count === undefined ? 'other' : rules.select(count)
  return entry[cat] ?? entry.other
}

/** 번역이 채워진 항목인가 — 빈 문자열·빈 객체는 "아직 안 됨" 이다. */
export function isTranslated(entry: Entry | undefined): entry is Entry {
  if (entry === undefined) return false
  if (typeof entry === 'string') return entry !== ''
  return Object.values(entry).some((v) => !!v)
}

// ── 백엔드 문장 ──
//
// 백엔드(게이트웨이·데몬)는 **한국어 문장을 그대로** 만든다. 문장마다 코드를 붙이는 대신
// 번역 파일의 `{이름}` 형 키를 **패턴**으로 보고, 받은 문장이 어느 키에 맞는지 찾는다
// (`f"{cam_id} 가 연결돼 있지 않습니다"` → 키 `{cam_id} 가 연결돼 있지 않습니다`).
// 못 찾으면 원문을 보여 준다 — 틀린 번역을 보여 주느니 한국어가 낫다.

export type Matcher = {
  re: RegExp
  names: string[]
  /** 자리표시자가 아닌 글자 수. 클수록 구체적이라 먼저 맞춘다 */
  weight: number
  entry: Entry
  lang: string
}

const escapeRe = (s: string) => s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')

export function buildMatchers(catalog: Catalog, lang: string): Matcher[] {
  const out: Matcher[] = []
  for (const [key, entry] of Object.entries(catalog)) {
    if (!isTranslated(entry) || !(HAS_PLACEHOLDER.test(key) || /\{\{|\}\}/.test(key))) continue
    const parts = splitTemplate(key)
    const names: string[] = []
    let src = '^'
    let weight = 0
    parts.forEach((p, i) => {
      if (i % 2 === 0) { src += escapeRe(p); weight += p.length }
      else { names.push(p); src += '([\\s\\S]+?)' }
    })
    out.push({ re: new RegExp(src + '$'), names, weight, entry, lang })
  }
  return out.sort((a, b) => b.weight - a.weight)
}

export function matchSentence(
  text: string,
  matchers: Matcher[],
): { entry: Entry; lang: string; params: Params } | null {
  for (const m of matchers) {
    const hit = m.re.exec(text)
    if (!hit) continue
    const params: Params = {}
    m.names.forEach((n, i) => { if (!(n in params)) params[n] = hit[i + 1] })
    return { entry: m.entry, lang: m.lang, params }
  }
  return null
}
