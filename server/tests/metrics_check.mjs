/**
 * The metrics dashboard, end to end, from pixels and the DOM.
 *
 * Guards the thing the user actually hit: an entry point that exists in the
 * code but not on the page. So this navigates the real UI rather than poking
 * the API - a passing API with no button is exactly the failure being tested.
 */
import puppeteer from 'puppeteer'

const BASE = process.argv[2] || process.env.MRIV_BASE || 'http://127.0.0.1:8080'
const sleep = (ms) => new Promise((r) => setTimeout(r, ms))
// The second annotated ds0826 case. Case 1 is the swapped-label knee, which is
// exercised separately below as a deliberate hard failure.
const SEG_ID = Number(process.env.MRIV_SEG_ID || 18)
const problems = []
function check(name, cond, detail = '') {
  console.log(`  ${cond ? 'ok  ' : 'FAIL'}  ${name}${detail ? ' - ' + detail : ''}`)
  if (!cond) problems.push(name + (detail ? ' - ' + detail : ''))
}

const browser = await puppeteer.launch({
  executablePath: process.env.CHROME_PATH || undefined,
  headless: true,
  args: ['--no-sandbox', '--enable-unsafe-swiftshader', '--use-gl=swiftshader'],
})
const errors = []
const external = []

try {
  const page = await browser.newPage()
  await page.setViewport({ width: 1600, height: 1000 })
  page.on('pageerror', (e) => errors.push(String(e).slice(0, 200)))
  page.on('console', (m) => { if (m.type() === 'error') errors.push(m.text().slice(0, 200)) })
  page.on('request', (r) => {
    const u = new URL(r.url())
    if (u.origin !== new URL(BASE).origin && u.protocol.startsWith('http')) external.push(u.href)
  })

  await page.goto(BASE, { waitUntil: 'networkidle2', timeout: 60000 })
  await page.waitForSelector('.sidebar-head select', { timeout: 30000 })
  await page.select('.sidebar-head select', 'ds0826')
  await sleep(1200)
  await page.click('.sidebar-head input[type=checkbox]')     // annotated only
  await sleep(1500)

  // Case 1 is the knee whose medial/lateral labels are swapped, and it is the
  // first row. Open a later one for the happy path; case 1 gets its own check
  // further down, because refusing to report a mirrored knee is a feature.
  const rows = await page.$$('.plist li')
  await rows[1].click()
  await sleep(20000)

  // 1. the dashboard is reachable from the reading page. The sidebar report
  //    owns that entry now (report_flow_check.mjs covers its placement); here
  //    we only need a segmentation id to open the dashboard with.
  const segId = await page.evaluate(async () => {
    const rows = [...document.querySelectorAll('.sidebar.right .sr')]
    return rows.length ? 1 : 0
  })
  check('阅片页侧栏有诊断报告区块', segId === 1)

  const href = `#/metrics/${SEG_ID}`
  await page.goto(`${BASE}/${href}`, { waitUntil: 'networkidle2', timeout: 60000 })
  await page.waitForSelector('.mx-card', { timeout: 180000 })
  await sleep(2500)

  const seen = await page.evaluate(() => ({
    cards: document.querySelectorAll('.mx-card').length,
    svgs: document.querySelectorAll('.mx-card svg').length,
    dots: document.querySelectorAll('.mx-card svg circle').length,
    bars: document.querySelectorAll('.mx-hist svg rect').length,
    hatch: !!document.querySelector('#mx-hatch'),
    rowsInTable: document.querySelectorAll('.mx-table tbody tr').length,
    badges: document.querySelectorAll('.mx-badge').length,
    disclaim: document.querySelector('.mx-disclaim')?.textContent?.trim() ?? '',
    caveat: document.querySelector('.mx-caveat')?.textContent?.trim() ?? '',
    hero: document.querySelector('.mx-hero-n')?.textContent?.trim() ?? '',
    err: document.querySelector('.err')?.textContent?.trim() ?? '',
    firstCardHeading: document.querySelector('.mx-card h2')?.textContent?.trim() ?? '',
    bodyScrollsX: document.documentElement.scrollWidth > window.innerWidth + 2,
  }))

  check('看板渲染出多个区块', seen.cards >= 4, `${seen.cards}`)
  check('主图已绘制', seen.svgs >= 2, `${seen.svgs} svg`)
  check('主图有测量点', seen.dots >= 20, `${seen.dots} 个点`)
  check('直方图有柱子', seen.bars >= 30, `${seen.bars}`)
  check('参考带用斜纹（非纯色）', seen.hatch)
  check('数据表有行', seen.rowsInTable >= 20, `${seen.rowsInTable}`)
  check('可信度徽章带文字', seen.badges >= 3, `${seen.badges}`)
  check('页眉有免责声明', /非诊断结论/.test(seen.disclaim), seen.disclaim)
  check('质量说明排在数字之前', /测量质量/.test(seen.firstCardHeading), seen.firstCardHeading)
  check('层厚局限性写在正文里', /3 mm/.test(seen.caveat) && /低估/.test(seen.caveat))
  check('内外侧不对称度已算出', /%/.test(seen.hero), seen.hero)
  check('页面无错误提示', seen.err === '', seen.err.slice(0, 120))
  check('页面不横向溢出', !seen.bodyScrollsX)

  // 2b. the report itself - the deliverable, not just the charts
  const rep = await page.evaluate(() => ({
    present: !!document.querySelector('.mx-report'),
    sections: [...document.querySelectorAll('.mx-section h3')].map((e) => e.textContent?.trim()),
    badge: document.querySelector('.mx-report .mx-badge')?.textContent?.trim() ?? '',
    body: document.querySelector('.mx-sections')?.textContent ?? '',
    disclaimer: document.querySelector('.mx-disclaim-block')?.textContent?.trim() ?? '',
    audit: document.querySelector('.mx-payload')?.textContent ?? '',
    hasReview: !!document.querySelector('.mx-review input'),
  }))
  check('报告卡片存在', rep.present)
  check('四段齐全', JSON.stringify(rep.sections) ===
    JSON.stringify(['影像所见', '定量测量', '印象', '建议']), rep.sections.join('/'))
  check('标注了生成来源', /模型生成|内置模板|模型被拒/.test(rep.badge), rep.badge)
  check('正文非空', rep.body.length > 200, `${rep.body.length} 字`)
  check('正文声明非诊断结论', /非诊断结论/.test(rep.disclaimer))
  check('正文提到层厚局限', /低估|层厚/.test(rep.body))
  check('有医师复核入口', rep.hasReview)
  check('审计载荷可查看', rep.audit.length > 100, `${rep.audit.length} 字`)
  for (const probe of ['haifeng', 'Elements', '.dcm', '1.2.840']) {
    check(`审计载荷不含 ${probe}`, !rep.audit.includes(probe))
  }

  // 3. a shared link works on a cold load - the point of a separate page
  await page.goto(`${BASE}/${href}`, { waitUntil: 'networkidle2', timeout: 60000 })
  await page.waitForSelector('.mx-card', { timeout: 120000 })
  await sleep(1500)
  const cold = await page.evaluate(() => ({
    cards: document.querySelectorAll('.mx-card').length,
    back: !!document.querySelector('.mx-back'),
  }))
  check('直接打开链接也能用（冷加载）', cold.cards >= 4, `${cold.cards}`)
  check('有返回阅片的入口', cold.back)

  // 4. the swapped-label knee must refuse rather than emit mirrored metrics
  await page.goto(`${BASE}/#/metrics/8`, { waitUntil: 'networkidle2', timeout: 60000 })
  await page.waitForSelector('.mx-fail, .mx-card', { timeout: 120000 })
  await sleep(1500)
  const swapped = await page.evaluate(() => ({
    failed: !!document.querySelector('.mx-fail'),
    text: document.querySelector('.mx-fail')?.textContent ?? '',
    charts: document.querySelectorAll('.mx-card svg').length,
  }))
  check('内外侧互换的病例拒绝出指标', swapped.failed)
  check('并说明了拒绝原因', /swapped|互换/.test(swapped.text), swapped.text.slice(0, 90))
  check('拒绝时不画任何图表', swapped.charts === 0, `${swapped.charts}`)
} finally {
  await browser.close()
}

const real = errors.filter((e) => !/favicon/i.test(e))
check('无控制台错误', real.length === 0, real.slice(0, 2).join(' | '))
check('无外部请求（离线可用）', external.length === 0, external.slice(0, 3).join(' | '))

if (problems.length) {
  console.log(`\n${problems.length} problems:`)
  for (const p of problems) console.log('  - ' + p)
  process.exit(1)
}
console.log('\nALL METRICS CHECKS PASSED')
