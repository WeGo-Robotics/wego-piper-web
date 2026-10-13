#!/usr/bin/env node
/**
 * 프론트 소스에서 번역 대상 문장을 뽑는다 — `tools/i18n.py` 가 부른다.
 *
 * 대상은 **첫 인자가 글자 리터럴인** `t(…)` · `tr(…)` · `msg(…)` · `t.rich(…)` · `tr.rich(…)` 호출이다.
 * 정규식이 아니라 TypeScript 구문 트리로 읽는다 — 주석·문자열 속 `t(` 를 잘못 잡지 않는다.
 *
 * 출력(JSON, stdout): { keys: [{ text, file, line, via }], dynamic: [{ file, line, via, src }] }
 *   dynamic = 첫 인자가 리터럴이 아닌 `t(x)` / `tr(x)`. 틀린 게 아니라(`t(label)` 은 정상이다)
 *   원문이 어디서 오는지 사람이 볼 수 있게 남긴다.
 */
import fs from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import { createRequire } from 'node:module'

const here = path.dirname(fileURLToPath(import.meta.url))
const require = createRequire(import.meta.url)
const ts = require(path.join(here, '..', 'node_modules', 'typescript'))
const root = path.join(here, '..', 'src')

const files = []
;(function walk(d) {
  for (const e of fs.readdirSync(d, { withFileTypes: true })) {
    const p = path.join(d, e.name)
    if (e.isDirectory()) walk(p)
    else if (/\.tsx?$/.test(e.name) && !e.name.endsWith('.d.ts')) files.push(p)
  }
})(root)

const CALLEES = new Set(['t', 'tr', 'msg'])
const keys = []
const dynamic = []

for (const file of files.sort()) {
  const src = fs.readFileSync(file, 'utf8')
  const sf = ts.createSourceFile(file, src, ts.ScriptTarget.Latest, true,
    file.endsWith('x') ? ts.ScriptKind.TSX : ts.ScriptKind.TS)
  const rel = 'frontend/' + path.relative(path.join(here, '..'), file)
  const visit = (n) => {
    if (ts.isCallExpression(n) && n.arguments.length > 0) {
      const f = n.expression
      let via = null
      if (ts.isIdentifier(f) && CALLEES.has(f.text)) via = f.text
      else if (ts.isPropertyAccessExpression(f) && f.name.text === 'rich'
               && ts.isIdentifier(f.expression) && (f.expression.text === 't' || f.expression.text === 'tr'))
        via = f.expression.text + '.rich'
      if (via) {
        const a = n.arguments[0]
        const line = sf.getLineAndCharacterOfPosition(n.getStart()).line + 1
        if (ts.isStringLiteral(a) || ts.isNoSubstitutionTemplateLiteral(a)) {
          // `t('정렬', undefined, 'sort')` → 키 `sort::정렬`
          const c = n.arguments[2]
          const ctx = c && (ts.isStringLiteral(c) || ts.isNoSubstitutionTemplateLiteral(c)) && via !== 'msg' ? c.text : null
          keys.push({ text: ctx ? `${ctx}::${a.text}` : a.text, file: rel, line, via })
        } else if (via !== 'msg') {
          dynamic.push({ file: rel, line, via, src: a.getText().slice(0, 60) })
        }
      }
    }
    ts.forEachChild(n, visit)
  }
  visit(sf)
}
process.stdout.write(JSON.stringify({ keys, dynamic }))
