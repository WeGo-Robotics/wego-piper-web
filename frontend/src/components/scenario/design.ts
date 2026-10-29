import type { Design } from './types'

/**
 * 마지막 설계 — 시나리오 탭이 쓰고 수집 탭이 읽는다 (녹화를 그 설계의 계획으로 시작한다).
 * 수집 화면의 폼(`piper_record_settings`)과 같은 자리(브라우저)다. 이름 붙여 공유할 것은 프리셋으로,
 * 이건 "새로고침해도 하던 것 그대로" 를 위한 것이다.
 */
export const STORE_KEY = 'piper_scenario_design'

export function loadStoredDesign(): unknown {
  try { return JSON.parse(localStorage.getItem(STORE_KEY) || 'null') } catch { return null }
}

export function saveDesign(d: Design) {
  try { localStorage.setItem(STORE_KEY, JSON.stringify(d)) } catch { /* 사생활 모드 — 이번 세션엔 그대로 쓴다 */ }
}

/** 수집 탭이 녹화에 쓸 설계 — 모양이 안 맞으면(아직 시나리오 탭을 안 열었다) null. */
export function storedDesign(): Design | null {
  const d = loadStoredDesign() as Design | null
  return d && typeof d.template === 'string' && d.board && Array.isArray(d.catalog) && d.common && d.fields
    ? d : null
}

/** 설계 → 계획 요청 본문. `/scenario/generate` 와 `/recording/start` 의 `scenario` 가 같은 모양이다 —
 *  게이트웨이가 녹화 때 계획을 다시 만들므로 둘이 어긋나면 화면과 다른 계획을 찍는다. */
export function designRequest(d: Design) {
  const ids = new Set(d.catalog.map(i => i.id))
  const fixed = Object.fromEntries(
    Object.entries(d.common.fixed_composition ?? {}).filter(([k, v]) => ids.has(k) && v > 0))
  return {
    template: d.template, seed: d.seed, board: d.board, catalog: d.catalog,
    values: {
      ...d.fields,
      ...d.common,
      fixed_composition: fixed,
      nuisance: (d.common.nuisance ?? []).map(n => ({ name: n.name, levels: n.levels.filter(Boolean) })),
    },
  }
}
