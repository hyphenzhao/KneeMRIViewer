/**
 * The reading-room report workflow, driven through the real UI.
 *
 * What this guards, in order of how badly it has already gone wrong:
 *
 * 1. The entry point must be where the clinician looks - *below* the label
 *    list. It was previously above it, and before that it did not exist on this
 *    page at all while the API worked fine, so an API-level test proves nothing.
 * 2. 查看详细报告 must open a NEW WINDOW. Same-page hash navigation unmounts the
 *    viewer and throws away the loaded volume.
 * 3. A second click must reuse that window rather than spawn another.
 * 4. Settings must survive a reload - i.e. actually reach the database.
 *
 *   node tests/report_flow_check.mjs [baseUrl]
 */
import puppeteer from 'puppeteer'

const BASE = process.argv[2] || process.env.MRIV_BASE || 'http://127.0.0.1:8080'
const DATASET = process.env.MRIV_DATASET || 'ds0826'
const sleep = (ms) => new Promise((r) => setTimeout(r, ms))

/**
 * Replace an input's text. A triple-click does not reliably select inside an
 * input bound to a datalist, and React only sees real key events - so an
 * append-instead-of-replace here silently tests the wrong value.
 */
async function retype(page, handle, text) {
  await handle.click()
  await page.keyboard.down('Control')
  await page.keyboard.press('KeyA')
  await page.keyboard.up('Control')
  await page.keyboard.press('Backspace')
  await handle.type(text)
}

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
  page.on('console', (m) => {
    if (m.type() === 'error') errors.push(m.text().slice(0, 200))
  })
  page.on('request', (r) => {
    const u = new URL(r.url())
    if (u.origin !== new URL(BASE).origin && u.protocol.startsWith('http')) {
      external.push(u.href)
    }
  })

  await page.goto(BASE, { waitUntil: 'networkidle2', timeout: 60000 })
  await page.waitForSelector('.sidebar-head select', { timeout: 30000 })
  await page.select('.sidebar-head select', DATASET)
  await sleep(1200)
  await page.click('.sidebar-head input[type=checkbox]')      // annotated only
  await sleep(1500)

  // Case 1 is the knee whose medial/lateral labels are swapped; open a later one.
  const rows = await page.$$('.plist li')
  await rows[1].click()
  await sleep(20000)

  // ---- 1. the entry sits BELOW the label list -----------------------------
  const placement = await page.evaluate(() => {
    const labels = document.querySelector('.sidebar.right ul.labels')
    const report = document.querySelector('.sidebar.right .sr')
    if (!labels || !report) return { labels: !!labels, report: !!report }
    // DOCUMENT_POSITION_FOLLOWING === 4: report comes after the label list.
    return {
      labels: true, report: true,
      after: !!(labels.compareDocumentPosition(report) & 4),
      oldEntryGone: !document.querySelector('.mx-entry'),
    }
  })
  check('侧栏有分割标签列表', placement.labels)
  check('侧栏有诊断报告区块', placement.report)
  const sections = await page.$$eval('.sidebar.right .sr .section-title',
    (els) => els.map((e) => e.textContent?.trim().replace(/●.*$/, '').trim()))
  check('侧栏有「软骨报告」与「膝关节报告」两段', sections[0] === '软骨报告' && sections[1] === '膝关节报告',
    sections.join('/'))
  check('报告区块在分割标签之后', placement.after === true)
  check('旧的顶部入口已移除', placement.oldEntryGone === true)

  // ---- 2. generate, and render like the radiologist report ----------------
  const hadReport = await page.$('.sr:not(.sr-knee) dl.report')
  if (!hadReport) {
    await page.click('.sr:not(.sr-knee) .sr-primary')
    await page.waitForSelector('.sr:not(.sr-knee) dl.report', { timeout: 180000 })
  }
  await sleep(1000)

  const card = await page.evaluate(() => {
    const titles = [...document.querySelectorAll('.sr:not(.sr-knee) dl.report dt')].map((e) => e.textContent)
    const dds = [...document.querySelectorAll('.sr:not(.sr-knee) dl.report dd')].map((e) => e.textContent ?? '')
    return {
      titles,
      shortest: Math.min(...dds.map((d) => d.trim().length)),
      usesReportClass: !!document.querySelector('.sr:not(.sr-knee) dl.report'),
      badge: document.querySelector('.sr-badge')?.textContent?.trim() ?? '',
      buttons: [...document.querySelectorAll('.sr:not(.sr-knee) .sr-buttons button')].map((b) => b.textContent?.trim()),
      disclaim: document.querySelector('.sr:not(.sr-knee) .sr-foot .sr-hint')?.textContent ?? '',
    }
  })
  check('四段齐全', JSON.stringify(card.titles) ===
    JSON.stringify(['影像所见', '定量测量', '印象', '建议']), card.titles.join('/'))
  check('每段都有内容', card.shortest > 10, `最短 ${card.shortest} 字`)
  check('复用影像报告的 dl.report 样式', card.usesReportClass)
  check('标注了生成来源', /模型生成|内置模板|模型被拒/.test(card.badge), card.badge)
  check('两个按钮齐全', JSON.stringify(card.buttons) ===
    JSON.stringify(['重新生成报告', '查看详细报告']), card.buttons.join('/'))
  check('注明非诊断结论', /非诊断结论/.test(card.disclaim))

  // ---- 3. 查看详细报告 opens a popup, and reuses it -----------------------
  const before = (await browser.targets()).length
  await page.evaluate(() => {
    [...document.querySelectorAll('.sr-buttons button')]
      .find((b) => b.textContent?.includes('查看详细报告'))?.click()
  })
  const popupTarget = await browser.waitForTarget(
    (t) => t.url().includes('#/report/'), { timeout: 30000 })
  check('详细报告开了新窗口', !!popupTarget, popupTarget?.url() ?? '')
  check('新窗口用的是 hash 路由', /#\/report\/\d+/.test(popupTarget.url()),
    popupTarget.url())

  const popup = await popupTarget.page()
  await popup.waitForSelector('[data-report-ready="1"]', { timeout: 240000 })
  const rep2 = await popup.evaluate(() => {
    const titles = [...document.querySelectorAll('.rp-chapter h3, .rp-chapter h2')]
      .map((e) => e.textContent?.replace(/\s+/g, '').replace(/(算法生成|模型解读|未评估|生成失败)$/, ''))
    const pending = [...document.querySelectorAll('.rp-chapter-pending')]
    return {
      titles,
      pendingCount: pending.length,
      pendingText: pending.map((e) => e.textContent ?? '').join(' '),
      radiologistLabels: document.querySelectorAll('.rp-radiologist-label, .rp-origin').length,
      disclaimer: document.querySelector('.rp-foot')?.textContent ?? '',
    }
  })
  check('新窗口里章节报告已渲染', rep2.titles.length >= 11, rep2.titles.join('/'))
  check('章节按本院顺序', rep2.titles.slice(0, 3).join('/') === '检查概况与数据质量/骨皮质与骨髓信号/关节软骨',
    rep2.titles.slice(0, 3).join('/'))
  check('未接入模型的章节显示占位', rep2.pendingCount >= 5 && /未评估/.test(rep2.pendingText), `${rep2.pendingCount}`)
  check('占位章节不断言正常', !/未见异常|光整|形态可/.test(rep2.pendingText))
  check('放射科内容带标签', rep2.radiologistLabels >= 1, `${rep2.radiologistLabels}`)
  check('页脚免责声明', /非诊断结论/.test(rep2.disclaimer))

  // A second click must land in the same named window, not open another.
  await page.bringToFront()
  await page.evaluate(() => {
    [...document.querySelectorAll('.sr-buttons button')]
      .find((b) => b.textContent?.includes('查看详细报告'))?.click()
  })
  await sleep(2500)
  const after = (await browser.targets()).length
  check('重复点击复用同一个窗口', after <= before + 1, `${before} → ${after}`)
  await popup.close()

  // ---- 4. AI 管理 ---------------------------------------------------------
  await page.bringToFront()
  await page.evaluate(() => {
    [...document.querySelectorAll('.topbar button')]
      .find((b) => b.textContent?.includes('AI 管理'))?.click()
  })
  await page.waitForSelector('.modal', { timeout: 15000 })
  await sleep(800)

  const panel = await page.evaluate(() => ({
    warn: document.querySelector('.modal-warn')?.textContent ?? '',
    fields: [...document.querySelectorAll('.modal .fld > span')].map((e) => e.textContent?.trim()),
    keyIsPassword: document.querySelector('.modal input[type=password]') !== null,
  }))
  check('AI 管理面板打开', panel.fields.length > 0, panel.fields.join('/'))
  check('面板声明了无认证风险', /没有登录认证|无登录认证/.test(panel.warn))
  check('有接口地址与模型字段',
    panel.fields.includes('接口地址') && panel.fields.includes('模型'))
  check('API Key 用密码框', panel.keyIsPassword)

  // Change the model, save, reload, and confirm it stuck.
  const modelInput = await page.$('.modal .fld input[list=mriv-models]')
  await retype(page, modelInput, 'deepseek-flash')
  await page.evaluate(() => {
    [...document.querySelectorAll('.modal-foot button')]
      .find((b) => b.textContent?.trim() === '保存')?.click()
  })
  await page.waitForSelector('.modal-msg', { timeout: 20000 })

  await page.reload({ waitUntil: 'networkidle2', timeout: 60000 })
  await sleep(1500)
  await page.evaluate(() => {
    [...document.querySelectorAll('.topbar button')]
      .find((b) => b.textContent?.includes('AI 管理'))?.click()
  })
  await page.waitForSelector('.modal .fld input[list=mriv-models]', { timeout: 15000 })
  const persisted = await page.$eval('.modal .fld input[list=mriv-models]', (e) => e.value)
  check('设置保存后刷新仍在', persisted === 'deepseek-flash', persisted)

  // Put it back so the next run starts from the same place.
  const restore = await page.$('.modal .fld input[list=mriv-models]')
  await retype(page, restore, process.env.MRIV_MODEL || 'deepseek-v4-pro')
  await page.evaluate(() => {
    [...document.querySelectorAll('.modal-foot button')]
      .find((b) => b.textContent?.trim() === '保存')?.click()
  })
  await page.waitForSelector('.modal-msg', { timeout: 20000 })
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
console.log('\nALL REPORT FLOW CHECKS PASSED')
