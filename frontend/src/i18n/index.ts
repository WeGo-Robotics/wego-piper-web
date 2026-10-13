/**
 * 다국어 — 공개 표면. 설계와 언어 추가법은 `feature/i18n.md`.
 *
 *   const t = useT()                       // 컴포넌트 안에서
 *   t('로봇 등록')                          // → "Register robot" (en 선택 시)
 *   t('에피소드 {n}개', { n: 3 })
 *   t.rich('{link} 를 확인하세요', { link: <a href="…">문서</a> })   // JSX 를 끼울 때
 *
 *   tr('저장 실패')                         // 컴포넌트 밖 — 호출한 순간의 언어
 *   msg('실행 중')                          // 모듈 상수의 표지. 그릴 때 t(x) 로 번역한다
 *   tb(res.error)                          // 백엔드가 보낸 한국어 문장
 *
 * ⚠ **언어를 바꿔도 화면을 다시 띄우지 않는다.** 새로고침이나 루트 재마운트는 WS 를
 *   끊고, 그 틈에 E-stop heartbeat 가 2초를 넘기면 녹화·추론이 SIGKILL 된다. 바뀐
 *   언어는 `useT()` 를 쓴 컴포넌트만 다시 그린다 — 그래서 컴포넌트 안에서는 `tr` 이
 *   아니라 `useT()` 의 `t` 를 쓴다 (`tr` 은 그릴 때 다시 안 불리면 낡은 언어가 남는다).
 */

import { createElement, Fragment, useSyncExternalStore } from 'react'
import type { ReactNode } from 'react'
import {
  HANGUL,
  buildMatchers,
  contextKey,
  interpolate,
  isTranslated,
  matchSentence,
  pick,
  splitTemplate,
} from './core'
import type { Catalog, Matcher, Params } from './core'

export type { Catalog, Params } from './core'

/** 코드에 적힌 원문의 언어. 번역 파일이 없다 — 키가 곧 값이다. */
export const SOURCE_LANG = 'ko'

const STORAGE_KEY = 'piper.lang'

/**
 * 번역 파일은 `locales/<코드>.json` — **파일을 놓는 것이 곧 언어 추가다.**
 * 목록을 따로 적지 않는다 (적으면 파일만 넣고 목록을 빠뜨린 언어가 조용히 안 뜬다).
 * 언어 하나만 내려받으므로 파일이 늘어도 번들이 불지 않는다.
 */
const loaders = import.meta.glob<{ default: Catalog }>('./locales/*.json')
const FILE_LANGS = Object.keys(loaders)
  .map((p) => /\/([^/]+)\.json$/.exec(p)?.[1] ?? '')
  .filter(Boolean)

export const LANGUAGES: readonly string[] = [
  SOURCE_LANG,
  ...FILE_LANGS.filter((c) => c !== SOURCE_LANG).sort(),
]

/** 그 언어의 **자기 이름** (`ja` → 日本語). 목록에서 못 읽는 언어를 못 찾으면 안 되니 자국어로 쓴다. */
export function languageName(code: string): string {
  try {
    const n = new Intl.DisplayNames([code], { type: 'language' }).of(code)
    if (n && n !== code) return n.charAt(0).toLocaleUpperCase(code) + n.slice(1)
  } catch { /* 코드가 이상하면 코드를 그대로 */ }
  return code
}

// ── 상태 ──

type Layer = { lang: string; catalog: Catalog }

export interface TFn {
  /** `ctx` — 같은 한국어가 문맥으로 갈릴 때만(`i18n.md`). 번역 파일 키는 `ctx::원문` */
  (text: string, params?: Params, ctx?: string): string
  /** 자리표시자에 JSX 를 끼운다. 결과는 ReactNode 라 그대로 JSX 안에 놓는다. */
  rich: (text: string, params: Record<string, ReactNode>) => ReactNode
  readonly lang: string
}

type State = {
  lang: string
  /** 우선순위 순 — 고른 언어 → 같은 계열 → en. 비어 있으면(ko) 원문 그대로다 */
  layers: Layer[]
  t: TFn
  matchers: Matcher[] | null
  sentences: Map<string, string>
}

const loaded = new Map<string, Catalog>()
const listeners = new Set<() => void>()

function layersFor(code: string): string[] {
  if (code === SOURCE_LANG) return []
  const chain = [code]
  const base = code.split('-')[0]
  if (base !== code && FILE_LANGS.includes(base)) chain.push(base)
  // 고른 언어에 없는 문장은 한국어보다 영어가 낫다 — 한국어를 못 읽는 사람이 고른 언어다
  if (!chain.includes('en') && FILE_LANGS.includes('en')) chain.push('en')
  return chain
}

async function load(code: string): Promise<Catalog> {
  const hit = loaded.get(code)
  if (hit) return hit
  const loader = loaders[`./locales/${code}.json`]
  if (!loader) return {}
  const cat = (await loader()).default
  loaded.set(code, cat)
  return cat
}

/** 복수형을 고를 개수 — `count` 인자, 없으면 **첫 숫자 인자**. 번역자가 코드를 안 고치고 단수·복수를 줄 수 있게 한다. */
function countOf(params?: Record<string, unknown>): number | undefined {
  if (!params) return undefined
  if (typeof params.count === 'number') return params.count
  for (const v of Object.values(params)) {
    if (typeof v === 'number') return v
    if (typeof v === 'string' && /^-?\d+(\.\d+)?$/.test(v)) return Number(v)
  }
  return undefined
}

function resolve(layers: Layer[], text: string, params?: Record<string, unknown>, ctx?: string): string {
  // 문맥 항목이 어느 층에든 있으면 그것이 먼저다 — 일본어에 없고 영어에 있으면 영어가 낫다
  if (ctx) {
    const key = contextKey(ctx, text)
    for (const l of layers) {
      const e = l.catalog[key]
      if (isTranslated(e)) return pick(e, countOf(params), l.lang) ?? text
    }
  }
  for (const l of layers) {
    const e = l.catalog[text]
    if (isTranslated(e)) return pick(e, countOf(params), l.lang) ?? text
  }
  return text
}

function makeState(lang: string, layers: Layer[]): State {
  const base = (text: string, params?: Params, ctx?: string): string => {
    if (layers.length === 0) return params ? interpolate(text, params) : text
    const tpl = resolve(layers, text, params, ctx)
    return params ? interpolate(tpl, params) : tpl
  }
  const t = base as TFn
  t.rich = (text, params) => {
    const tpl = resolve(layers, text, params)
    return splitTemplate(tpl).map((part, i) =>
      i % 2 === 0
        ? part
        : createElement(Fragment, { key: i }, part in params ? params[part] : `{${part}}`),
    )
  }
  Object.defineProperty(t, 'lang', { value: lang })
  return { lang, layers, t, matchers: null, sentences: new Map() }
}

let current: State = makeState(SOURCE_LANG, [])

function commit(next: State) {
  current = next
  if (typeof document !== 'undefined') document.documentElement.lang = next.lang
  listeners.forEach((l) => l())
}

// ── 언어 고르기 ──

function normalize(code: string | null | undefined): string | null {
  if (!code) return null
  if (LANGUAGES.includes(code)) return code
  const base = code.split('-')[0]
  return LANGUAGES.includes(base) ? base : null
}

function storedLanguage(): string | null {
  try { return normalize(localStorage.getItem(STORAGE_KEY)) } catch { return null }
}

async function activate(code: string) {
  const names = layersFor(code)
  const layers: Layer[] = []
  for (const n of names) layers.push({ lang: n, catalog: await load(n) })
  commit(makeState(code, layers))
}

/**
 * 첫 화면 전에 한 번 부른다 — 번역이 실리기 전에 한국어가 번쩍이지 않게.
 *
 * 우선순위: `?lang=` (이번 한 번) → 저장한 선택 → **한국어**. 브라우저 언어로 자동
 * 전환하지 않는다 — 릴리스를 받는 순간 현장 PC 의 화면 언어가 바뀌면 안 된다.
 */
export async function initI18n(): Promise<void> {
  let code: string | null = null
  try { code = normalize(new URLSearchParams(location.search).get('lang')) } catch { /* 주소를 못 읽음 */ }
  code ??= storedLanguage() ?? SOURCE_LANG
  try {
    await activate(code)
  } catch (e) {
    console.error('[i18n] 번역 파일을 읽지 못했습니다 — 한국어로 시작합니다', e)
  }
  // 다른 탭·조종 창에서 바꾸면 따라간다
  window.addEventListener('storage', (ev) => {
    if (ev.key !== STORAGE_KEY) return
    const next = normalize(ev.newValue) ?? SOURCE_LANG
    if (next !== current.lang) void activate(next).catch(() => {})
  })
}

export async function setLanguage(code: string): Promise<void> {
  const next = normalize(code)
  if (!next) throw new Error(`지원하지 않는 언어: ${code}`)
  if (next === current.lang) return
  await activate(next)
  try { localStorage.setItem(STORAGE_KEY, next) } catch { /* 저장 못 해도 이번 세션은 바뀐다 */ }
}

// ── 쓰는 쪽 ──

const subscribe = (cb: () => void) => {
  listeners.add(cb)
  return () => { listeners.delete(cb) }
}

/** 컴포넌트 안에서 쓰는 번역 함수. 언어가 바뀌면 이 훅을 쓴 컴포넌트가 다시 그려진다. */
export function useT(): TFn {
  return useSyncExternalStore(subscribe, () => current.t)
}

export function useLanguage(): { lang: string; languages: readonly string[]; setLanguage: typeof setLanguage } {
  const t = useT()
  return { lang: t.lang, languages: LANGUAGES, setLanguage }
}

/** 컴포넌트 밖(이벤트 핸들러·서비스)에서 — 부른 순간의 언어로 옮긴다. 갱신은 구독하지 않는다. */
export const tr = Object.assign(
  (text: string, params?: Params, ctx?: string): string => current.t(text, params, ctx),
  { rich: (text: string, params: Record<string, ReactNode>): ReactNode => current.t.rich(text, params) },
)

/**
 * 모듈 상수의 표지. 값은 그대로 돌려주고, **번역은 그릴 때** `t(x)` 로 한다.
 * (모듈이 읽히는 시점의 언어로 굳어 버리면 안 된다.) 추출 도구가 이 호출을 본다.
 */
export const msg = <T extends string>(text: T): T => text

/**
 * 백엔드가 보낸 한국어 문장을 옮긴다. 번역 파일에서 같은 문장이나 같은 **모양**
 * (`{cam_id} 가 연결돼 있지 않습니다`)을 찾고, 없으면 원문을 돌려준다.
 *
 * 사용자 데이터(데이터셋 이름·작업 설명·로봇 별칭)에는 쓰지 않는다 — 우연히 키와 같으면
 * 사용자가 쓴 글자가 바뀐다.
 */
export function tb<T extends string | null | undefined>(text: T): T {
  if (!text || current.layers.length === 0 || !HANGUL.test(text)) return text
  const s = current
  const cached = s.sentences.get(text)
  if (cached !== undefined) return cached as T

  let out: string = text
  const exact = resolve(s.layers, text)
  if (exact !== text) {
    out = exact
  } else {
    s.matchers ??= s.layers
      .flatMap((l) => buildMatchers(l.catalog, l.lang))
      .sort((a, b) => b.weight - a.weight)
    const hit = matchSentence(text, s.matchers)
    if (hit) {
      const tpl = pick(hit.entry, countOf(hit.params), hit.lang)
      if (tpl) out = interpolate(tpl, hit.params)
    }
  }
  if (s.sentences.size > 2000) s.sentences.clear()   // 매번 달라지는 문장(초 단위 카운터)이 쌓이지 않게
  s.sentences.set(text, out)
  return out as T
}

/**
 * 사람에게 보이는 글자가 담기는 키. 응답·WS 메시지의 **이 키들**만 `tb` 로 옮긴다.
 *
 * ⚠ 사용자 데이터가 담기는 키(`name` · `label` · `task` · `description` · `alias`)는 넣지 않는다 —
 *   데이터셋 이름이나 작업 설명이 우연히 문장 모양과 맞으면 사용자가 쓴 글자가 바뀐다.
 *   새 키가 필요하면 여기에 더한다(백엔드가 그 키에 시스템 문장만 넣는다는 걸 확인하고).
 */
const TEXT_KEYS = new Set([
  'error', 'errors', 'warning', 'warnings', 'usb_warning', 'hint', 'reason', 'why',
  'message', 'detail', 'note', 'text', 'help', 'summary', 'status_text',
])

/**
 * 받은 JSON 에서 `TEXT_KEYS` 의 문장을 제자리에서 옮긴다 — 화면마다 `tb` 를 거는 대신
 * 들어오는 길목(api · WS)에서 한 번. 한국어일 때는 아무것도 안 한다.
 */
export function tbDeep<T>(root: T): T {
  if (current.layers.length === 0) return root
  const walk = (v: unknown): void => {
    if (Array.isArray(v)) {
      for (const x of v) if (x && typeof x === 'object') walk(x)
      return
    }
    if (!v || typeof v !== 'object') return
    const o = v as Record<string, unknown>
    for (const k of Object.keys(o)) {
      const x = o[k]
      if (TEXT_KEYS.has(k) && typeof x === 'string') o[k] = tb(x)
      else if (TEXT_KEYS.has(k) && Array.isArray(x)) o[k] = x.map((i) => (typeof i === 'string' ? tb(i) : (walk(i), i)))
      else if (x && typeof x === 'object') walk(x)
    }
  }
  walk(root)
  return root
}

// ── 날짜·숫자 ──
//
// 언어마다 모양이 다르다. 고른 언어의 규칙으로 쓴다 — `toLocaleString('ko-KR')` 을 직접
// 적으면 영어 화면에 한국식 날짜가 남는다.

export const fmt = {
  number: (n: number, opts?: Intl.NumberFormatOptions) =>
    new Intl.NumberFormat(current.lang, opts).format(n),
  date: (d: Date | number | string, opts?: Intl.DateTimeFormatOptions) =>
    new Date(d).toLocaleDateString(current.lang, opts),
  time: (d: Date | number | string, opts?: Intl.DateTimeFormatOptions) =>
    new Date(d).toLocaleTimeString(current.lang, opts),
  dateTime: (d: Date | number | string, opts?: Intl.DateTimeFormatOptions) =>
    new Date(d).toLocaleString(current.lang, opts),
}
