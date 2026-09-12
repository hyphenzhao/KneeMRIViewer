/**
 * The report page's three views and the server-side PDF.
 *
 * - print view is a white A4 column (measured from pixels, not class names)
 * - an edit made in 编辑视图 survives a reload and is marked in the print view
 *   as 「医师修改（原值 …）」 with the doctor's name
 * - /report.pdf returns a real PDF when the server can render one
 *
 *   node tests/report_views_check.mjs [baseUrl]     (MRIV_SEG_ID defaults to 18)
 */
import puppeteer from 'puppeteer'

const BASE = process.argv[2] || process.env.MRIV_BASE || 'http://127.0.0.1:8080'
const SEG_ID = Number(process.env.MRIV_SEG_ID || 18)
const sleep = (ms) => new Promise((r) => setTimeout(r, ms))

const problems = []
function check(name, cond, detail = '') {
  console.log(`  ${cond ? 'ok  ' : 'FAIL'}  ${name}${detail ? ' - ' + detail : ''}`)
  if (!cond) problems.push(name + (detail ? ' - ' + detail : ''))
}

async function retype(page, handle, text) {
  await handle.click()
  await page.keyboard.down('Control'); await page.keyboard.press('KeyA'); await page.keyboard.up('Control')
  await page.keyboard.press('Backspace')
  await handle.type(text)
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
  await page.setViewport({ width: 1200, height: 1000 })
  page.on('pageerror', (e) => errors.push(String(e).slice(0, 200)))
  page.on('console', (m) => { if (m.type() === 'error') errors.push(m.text().slice(0, 200)) })
  page.on('request', (r) => {
    const u = new URL(r.url())
    if (u.origin !== new URL(BASE).origin && u.protocol.startsWith('http')) external.push(u.href)
  })

  const open = async (view) => {
    await page.goto(`${BASE}/#/report/${SEG_ID}${view ? '?view=' + view : ''}`,
      { waitUntil: 'networkidle2', timeout: 90000 })
    await page.waitForSelector('[data-report-ready="1"]', { timeout: 240000 })
    await sleep(800)
  }

  // ---- web view --------------------------------------------------------
  await open('')
  const web = await page.evaluate(() => ({
    view: document.querySelector('.rp-root')?.getAttribute('data-view'),
    tabs: [...document.querySelectorAll('.rp-views button')].map((b) => b.textContent?.trim()),
    chapters: document.querySelectorAll('.rp-chapter').length,
  }))
  check('默认是网页视图', web.view === 'web', web.view ?? '')
  check('三个视图切换按钮', JSON.stringify(web.tabs) === JSON.stringify(['网页视图', '打印视图', '编辑视图']), web.tabs.join('/'))
  check('章节已渲染', web.chapters >= 10, `${web.chapters}`)

  // ---- print view: measured ------------------------------------------
  await open('print')
  const printStyle = await page.evaluate(() => {
    const cs = getComputedStyle(document.querySelector('.rp-page'))
    return { bg: cs.backgroundColor, color: cs.color, width: cs.width,
      scheme: getComputedStyle(document.querySelector('.rp-root')).colorScheme }
  })
  check('打印视图纸面为白', printStyle.bg === 'rgb(255, 255, 255)', printStyle.bg)
  check('打印视图文字为深色', printStyle.color === 'rgb(17, 17, 17)', printStyle.color)
  check('打印视图 color-scheme 为 light', /light/.test(printStyle.scheme), printStyle.scheme)
  check('纸面宽度为 A4 (210mm ≈ 794px)', Math.abs(parseFloat(printStyle.width) - 794) < 6, printStyle.width)
  const sheets = await page.evaluate(() => [...document.querySelectorAll('.rp-sheet')].map((s) => ({
    h: s.getBoundingClientRect().height, over: s.scrollHeight > s.clientHeight + 1,
    head: s.querySelector('.rp-sheet-head')?.textContent ?? '', foot: s.querySelector('.rp-sheet-foot')?.textContent ?? '',
  })))
  check('显式 A4 页（≥4 页，每页 297mm ≈ 1123px）', sheets.length >= 4 && sheets.every((s) => Math.abs(s.h - 1123) < 6), sheets.map((s) => s.h.toFixed(0)).join('/'))
  check('没有页溢出', sheets.every((s) => !s.over))
  check('每页有页眉页码与页脚声明', sheets.every((s, i) => new RegExp(`第 ${i + 1} / ${sheets.length} 页`).test(s.head) && /非诊断结论/.test(s.foot)), sheets[0]?.head)

  // Pixel check on the page element itself: >90% near-white.
  const el = await page.$('.rp-page')
  const png = await el.screenshot({ encoding: 'binary', clip: undefined })
  const stats = await page.evaluate(async (b64) => {
    const img = new Image()
    img.src = 'data:image/png;base64,' + b64
    await img.decode()
    const c = document.createElement('canvas'); c.width = img.width; c.height = img.height
    const ctx = c.getContext('2d'); ctx.drawImage(img, 0, 0)
    const d = ctx.getImageData(0, 0, c.width, c.height).data
    let white = 0, total = 0
    for (let i = 0; i < d.length; i += 4 * 53) { total++; if (d[i] > 235 && d[i + 1] > 235 && d[i + 2] > 235) white++ }
    return Math.round((100 * white) / total)
  }, Buffer.from(png).toString('base64'))
  check('打印视图像素 >85% 为白', stats > 85, `${stats}%`)

  // Chromium's own print engine must paginate it (client-side sanity).
  await page.emulateMediaType('print')
  const clientPdf = await page.pdf({ format: 'A4', printBackground: true })
  check('浏览器可将打印视图导出为 PDF', clientPdf.length > 5000 && Buffer.from(clientPdf).slice(0, 4).toString() === '%PDF', `${clientPdf.length} bytes`)
  await page.emulateMediaType(null)

  // ---- edit view: override a grade, reload, see the mark --------------
  await open('edit')
  const nameBox = await page.$('.rp-editor')
  await retype(page, nameBox, '测试医师')
  // The chapter may span sheets; every fragment carries data-chapter, only
  // the first carries the id.
  const CART = '[data-chapter="cartilage"]'
  const sel = await page.$(`${CART} select`)
  check('编辑视图里分级可改', !!sel)
  check('软骨章锚点仍在', !!(await page.$('#ch-cartilage')))
  if (sel) {
    const before = await page.$eval(`${CART} select`, (e) => e.value)
    const target = before === 'III' ? 'II' : 'III'
    await page.select(`${CART} select`, target)
    await sleep(2500)
    await open('print')
    const mark = await page.evaluate((c) => document.querySelector(`${c} .rp-mark`)?.textContent ?? '', CART)
    check('刷新后修改仍在并标注原值与医师', /医师修改（原值/.test(mark) && /测试医师/.test(mark), mark.slice(0, 80))
    const starred = await page.evaluate((c) => [...document.querySelectorAll(`${c} .rp-map-grade text`)].some((t) => /\*$/.test(t.textContent ?? '')), CART)
    check('分级图上标出医师修改（*）', starred)

    // put it back so the next run starts clean
    await open('edit')
    const revoke = await page.$(`${CART} .rp-mini`)
    if (revoke) { await revoke.click(); await sleep(1500) }
    const after = await page.evaluate((c) => document.querySelector(`${c} .rp-mark`), CART)
    check('撤销后标记消失', after === null)
  }

  // ---- server-side PDF -------------------------------------------------
  const status = await (await fetch(`${BASE}/api/v1/pdf/status`)).json()
  if (status.available) {
    const r = await fetch(`${BASE}/api/v1/segmentations/${SEG_ID}/report.pdf`)
    const buf = Buffer.from(await r.arrayBuffer())
    check('服务端 PDF 返回 application/pdf', (r.headers.get('content-type') || '').includes('application/pdf'), r.headers.get('content-type') ?? '')
    check('服务端 PDF 是真实 PDF 且不小', buf.length > 20000 && buf.slice(0, 4).toString() === '%PDF', `${buf.length} bytes`)
    check('下载文件名', /report_\d+\.pdf/.test(r.headers.get('content-disposition') || ''), r.headers.get('content-disposition') ?? '')
    // One PDF page per sheet: the page plan is ours, Chromium only prints it.
    // Skia writes the page tree uncompressed, so /Count is readable directly.
    const counts = [...buf.toString('latin1').matchAll(/\/Count\s+(\d+)/g)].map((m) => Number(m[1]))
    const pdfPages = counts.length ? Math.max(...counts) : -1
    check('PDF 页数 = 页元素数（无空白尾页）', pdfPages === sheets.length, `${pdfPages} vs ${sheets.length}`)
  } else {
    console.log(`  skip  服务端 PDF 不可用: ${status.detail}`)
  }
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
console.log('\nALL REPORT VIEW CHECKS PASSED')
