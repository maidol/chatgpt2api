// Codex OAuth 前端接线的契约检查：登录类型必须从下拉框一路传到 /api/accounts/oauth/start。
// vite build 不检查 .vue 模板里的标识符，拼错或漏传都能构建成功，所以单独查。
// 用法（仓库根目录，web-vue/node_modules 已装好）：node tests/check_codex_oauth_ui.mjs
import { readFileSync } from 'node:fs'
import { createRequire } from 'node:module'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')
const web = path.join(root, 'web-vue')
const require = createRequire(path.join(web, 'package.json'))
const ts = require('typescript')
const { parse, compileScript } = require('vue/compiler-sfc')

const failures = []
const check = (ok, message) => { if (!ok) failures.push(message) }
const read = (rel) => readFileSync(path.join(web, rel), 'utf8')
const source = (rel) => ts.createSourceFile(rel, read(rel), ts.ScriptTarget.Latest, true)

function find(node, predicate, out = []) {
  if (predicate(node)) out.push(node)
  // forEachChild 在回调返回真值时会停止遍历，所以这里不能返回 find 的结果
  ts.forEachChild(node, (child) => { find(child, predicate, out) })
  return out
}
const propNames = (obj) => obj.properties.map((p) => p.name && p.name.getText()).filter(Boolean)

// 1. API：startOAuthLogin 的请求体带 client
{
  const sf = source('src/api/accountImports.ts')
  const prop = find(sf, (n) => ts.isPropertyAssignment(n) && n.name.getText() === 'startOAuthLogin')[0]
  const bodies = prop ? find(prop, (n) => ts.isObjectLiteralExpression(n)) : []
  check(bodies.some((o) => propNames(o).includes('client') && propNames(o).includes('email_hint')),
    'api/accountImports.ts: startOAuthLogin 的请求体里没有 client')
}

// 2. runtime：调用 startOAuthLogin 时传 oauthClient.value，并且把 oauthClient 返回出去
{
  const sf = source('src/views/accounts/accountImportRuntime.ts')
  const calls = find(sf, (n) => ts.isCallExpression(n) && n.expression.getText() === 'accountImportsApi.startOAuthLogin')
  check(calls.length === 1 && calls[0].arguments.length === 2 && calls[0].arguments[1].getText() === 'oauthClient.value',
    'accountImportRuntime.ts: startOAuthLogin 没有把 oauthClient.value 作为第二个参数传出去')
  const fn = find(sf, (n) => ts.isFunctionDeclaration(n) && n.name?.getText() === 'useAccountImportRuntime')[0]
  const returns = fn ? find(fn.body, (n) => ts.isReturnStatement(n) && n.expression && ts.isObjectLiteralExpression(n.expression)) : []
  check(returns.some((r) => propNames(r.expression).includes('oauthClient')),
    'accountImportRuntime.ts: useAccountImportRuntime 的返回值里没有 oauthClient')
}

// 3. page：useAccountsPage 把 oauthClient 交给页面
{
  const sf = source('src/views/accounts/useAccountsPage.ts')
  const returns = find(sf, (n) => ts.isReturnStatement(n) && n.expression && ts.isObjectLiteralExpression(n.expression))
  check(returns.some((r) => propNames(r.expression).includes('oauthClient')),
    'useAccountsPage.ts: 返回值里没有 oauthClient')
}

// 4. 页面：oauthClient / oauthClientOptions 是 setup 绑定，并且模板里的下拉框用的是它们
{
  const { descriptor } = parse(read('src/views/Accounts.vue'))
  const bindings = compileScript(descriptor, { id: 'accounts' }).bindings || {}
  check('oauthClient' in bindings, 'Accounts.vue: setup 里没有 oauthClient 绑定（没从 useAccountsPage 解构出来）')
  check('oauthClientOptions' in bindings, 'Accounts.vue: setup 里没有 oauthClientOptions')
  const template = descriptor.template?.content || ''
  check(/:model-value="oauthClient"/.test(template) && /:options="oauthClientOptions"/.test(template),
    'Accounts.vue: 模板里没有绑定 oauthClient 的登录类型下拉框')
  check(/oauthClient = \$event === 'codex' \? 'codex' : 'web'/.test(template),
    'Accounts.vue: 下拉框选中后没有写回 oauthClient')
  const options = /const oauthClientOptions = \[([\s\S]*?)\]/.exec(descriptor.scriptSetup?.content || '')
  check(Boolean(options) && /value: 'codex'/.test(options[1]) && /value: 'web'/.test(options[1]),
    'Accounts.vue: oauthClientOptions 缺少 web 或 codex 选项')
}

if (failures.length) {
  for (const f of failures) console.log(`FAIL ${f}`)
  console.log(`UI CONTRACT: ${failures.length} FAILED`)
  process.exit(1)
}
console.log('UI CONTRACT: ALL 9 CHECKS PASSED')
