/**
 * The illustrated report, end to end, from pixels and the DOM.
 *
 * What it guards: that the figures are really on the page (painted canvases,
 * decoded images, a 3D snapshot with something in it), that they sit inside
 * the cartilage chapter rather than beside the report, that the page lays
 * itself out on A4 sheets none of which overflows, that the old dashboard
 * link redirects here, and that the swapped-label knee still refuses with
 * zero figures.
 *
 *   node tests/report_figures_check.mjs [baseUrl]     (MRIV_SEG_ID defaults to 18)
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

const browser = await puppeteer.launch({
  executablePath: process.env.CHROME_PATH || undefined,
  headless: true,
  args: ['--no-sandbox', '--enable-unsafe-swiftshader', '--use-gl=angle', '--use-angle=swiftshader', '--ignore-gpu-blocklist'],
})
const errors = []
const external = []

/** Fraction of an image's pixels that differ from its top-left corner colour. */
async function fillFraction(page, selector) {
  return page.evaluate(async (sel) => {
    const img = document.querySelector(sel)
    if (!img) return -1
    await img.decode().catch(() => undefined)
    const c = document.createElement('canvas'); c.width = img.naturalWidth; c.height = img.naturalHeight
    const ctx = c.getContext('2d'); ctx.drawImage(img, 0, 0)
    const d = ctx.getImageData(0, 0, c.width, c.height).data
    const r0 = d[0], g0 = d[1], b0 = d[2]
    let diff = 0, total = 0
    for (let i = 0; i < d.length; i += 4 * 37) {
      total++
      if (Math.abs(d[i] - r0) + Math.abs(d[i + 1] - g0) + Math.abs(d[i + 2] - b0) > 40) diff++
    }
    return diff / total
  }, selector)
}

try {
  const page = await browser.newPage()
  await page.setViewport({ width: 1300, height: 1000 })
  page.on('pageerror', (e) => errors.push(String(e).slice(0, 200)))
  page.on('console', (m) => { if (m.type() === 'error') errors.push(m.text().slice(0, 200)) })
  page.on('request', (r) => {
    const u = new URL(r.url())
    if (u.origin !== new URL(BASE).origin && u.protocol.startsWith('http')) external.push(u.href)
  })

  // ---- 1. the old dashboard link lands on the report -----------------------
  await page.goto(`${BASE}/#/metrics/${SEG_ID}`, { waitUntil: 'networkidle2', timeout: 90000 })
  await sleep(500)
  check('#/metrics 重定向到 #/report', page.url().includes(`#/report/${SEG_ID}`), page.url())
  await page.waitForSelector('[data-report-ready="1"]', { timeout: 300000 })
  await sleep(500)

  // ---- 2. figures inside the cartilage chapter -----------------------------
  const seen = await page.evaluate(() => {
    const cart = [...document.querySelectorAll('[data-chapter="cartilage"]')]
    const inCart = (sel) => cart.reduce((n, el) => n + el.querySelectorAll(sel).length, 0)
    const canvases = cart.flatMap((el) => [...el.querySelectorAll('.rp-map canvas')])
    let painted = 0
    for (const cv of canvases) {
      const d = cv.getContext('2d').getImageData(0, 0, cv.width, cv.height).data
      let opaque = 0
      for (let i = 3; i < d.length; i += 4 * 13) if (d[i] > 0) opaque++
      if (opaque > 50) painted++
    }
    const slices = cart.flatMap((el) => [...el.querySelectorAll('img.rp-slice-img')])
    const hatch = [...document.querySelectorAll('pattern')].map((p) => p.id)
    const firstNum = document.querySelector('.rp-sheet .rp-num')
    const caveat = document.querySelector('.rp-sheet .rp-caveat')
    return {
      chapterFragments: cart.length,
      thicknessMaps: inCart('.rp-map-thickness canvas'),
      gradeMaps: inCart('.rp-map-grade canvas'),
      paintedCanvases: painted, canvases: canvases.length,
      sliceImgs: slices.length,
      sliceLoaded: slices.filter((i) => i.complete && i.naturalWidth > 0).length,
      recon: !!cart.find((el) => el.querySelector('img[data-figure="recon3d"]')),
      dots: inCart('.rp-dotplot circle'),
      bars: inCart('.rp-hist rect'),
      hatchUnique: new Set(hatch).size === hatch.length && hatch.length >= 4,
      captions: inCart('figcaption'),
      figTitles: [...document.querySelectorAll('[data-chapter="cartilage"] .rp-fig-title')].map((e) => e.textContent.slice(0, 4)),
      caveatBeforeNumbers: !!(caveat && firstNum && (caveat.compareDocumentPosition(firstNum) & 4)),
      chapterHeadings: [...document.querySelectorAll('.rp-chapter h3, .rp-chapter h2')].slice(0, 3)
        .map((e) => e.textContent.replace(/\s+/g, '').replace(/(算法生成|模型解读|未评估|生成失败)$/, '')),
      h2h3InFigures: document.querySelectorAll('.rp-fig h2, .rp-fig h3').length,
      figuresTotal: Number(document.querySelector('.rp-root').dataset.figuresTotal),
      figuresSkipped: document.querySelector('.rp-root').dataset.figuresSkipped,
      figuresFailed: document.querySelector('.rp-root').dataset.figuresFailed,
      sheets: document.querySelectorAll('.rp-sheet').length,
      bodyScrollsX: document.documentElement.scrollWidth > window.innerWidth + 2,
      metricsLink: !!document.querySelector('a[href*="#/metrics/"]'),
    }
  })
  check('软骨章分成若干页片段', seen.chapterFragments >= 1, `${seen.chapterFragments}`)
  check('五张厚度展开图', seen.thicknessMaps === 5, `${seen.thicknessMaps}`)
  check('五张分级图', seen.gradeMaps === 5, `${seen.gradeMaps}`)
  check('所有地图画布已上色', seen.paintedCanvases === seen.canvases && seen.canvases === 10, `${seen.paintedCanvases}/${seen.canvases}`)
  check('三平面关键层已加载', seen.sliceImgs === 3 && seen.sliceLoaded === 3, `${seen.sliceLoaded}/${seen.sliceImgs}`)
  check('三维重建快照存在', seen.recon)
  check('亚区点图有测量点', seen.dots >= 20, `${seen.dots}`)
  check('直方图有柱子', seen.bars >= 30, `${seen.bars}`)
  check('斜纹 pattern id 唯一', seen.hatchUnique)
  check('图 1-5 按序出现', JSON.stringify(seen.figTitles) === JSON.stringify(['图 1 ', '图 2 ', '图 3 ', '图 4 ', '图 5 ']), seen.figTitles.join('/'))
  check('质量说明排在第一个数字之前', seen.caveatBeforeNumbers)
  check('前三章标题不变', seen.chapterHeadings.join('/') === '检查概况与数据质量/骨皮质与骨髓信号/关节软骨', seen.chapterHeadings.join('/'))
  check('图内不用 h2/h3 标题', seen.h2h3InFigures === 0, `${seen.h2h3InFigures}`)
  check('所有图形已登记且无跳过', seen.figuresTotal >= 14 && seen.figuresSkipped === '0' && seen.figuresFailed === '0',
    `total ${seen.figuresTotal} skipped ${seen.figuresSkipped} failed ${seen.figuresFailed}`)
  check('报告分成多张 A4 页', seen.sheets >= 4, `${seen.sheets}`)
  check('页面不横向溢出', !seen.bodyScrollsX)
  check('没有指向旧看板的链接', !seen.metricsLink)

  const recon = await fillFraction(page, 'img[data-figure="recon3d"]')
  check('三维快照不是空白', recon > 0.02, `${(recon * 100).toFixed(1)}% 非背景像素`)

  // ---- 3. print view: sheets fit, pixels white, sizes stable ---------------
  await page.goto(`${BASE}/#/report/${SEG_ID}?view=print`, { waitUntil: 'networkidle2', timeout: 90000 })
  await page.waitForSelector('[data-report-ready="1"]', { timeout: 300000 })
  await sleep(800)
  const sheetsScreen = await page.evaluate(() => [...document.querySelectorAll('.rp-sheet')].map((s) => ({
    h: s.getBoundingClientRect().height, over: s.scrollHeight > s.clientHeight + 1, flag: s.dataset.overflow === '1',
    used: s.querySelector('.rp-sheet-body')?.getBoundingClientRect().height ?? 0,
  })))
  check('打印视图每页高 297mm (≈1123px)', sheetsScreen.every((s) => Math.abs(s.h - 1123) < 6), sheetsScreen.map((s) => s.h.toFixed(0)).join('/'))
  check('没有页溢出', sheetsScreen.every((s) => !s.over && !s.flag), sheetsScreen.map((s) => (s.over ? 'over' : 'ok')).join('/'))
  console.log('      pages: ' + sheetsScreen.map((s) => `${Math.round(s.used / 3.7795)}mm`).join(' '))

  await page.emulateMediaType('print')
  await sleep(300)
  const sheetsPrint = await page.evaluate(() => [...document.querySelectorAll('.rp-sheet')].map((s) => s.querySelector('.rp-sheet-body')?.getBoundingClientRect().height ?? 0))
  await page.emulateMediaType(null)
  check('切换打印媒体后各页内容高度不变', sheetsPrint.every((h, i) => Math.abs(h - sheetsScreen[i].used) < 3),
    sheetsPrint.map((h, i) => (h - sheetsScreen[i].used).toFixed(0)).join('/'))

  const el = await page.$('.rp-sheet')
  const png = await el.screenshot({ encoding: 'binary' })
  const white = await page.evaluate(async (b64) => {
    const img = new Image(); img.src = 'data:image/png;base64,' + b64; await img.decode()
    const c = document.createElement('canvas'); c.width = img.width; c.height = img.height
    const ctx = c.getContext('2d'); ctx.drawImage(img, 0, 0)
    const d = ctx.getImageData(0, 0, c.width, c.height).data
    let w = 0, t = 0
    for (let i = 0; i < d.length; i += 4 * 53) { t++; if (d[i] > 235 && d[i + 1] > 235 && d[i + 2] > 235) w++ }
    return Math.round((100 * w) / t)
  }, Buffer.from(png).toString('base64'))
  check('打印视图第一页 >85% 为白', white > 85, `${white}%`)

  // ---- 4. the swapped-label knee: refuse, and draw nothing -----------------
  await page.goto(`${BASE}/#/report/8`, { waitUntil: 'networkidle2', timeout: 90000 })
  await page.waitForSelector('.rp-fail, [data-report-ready="1"]', { timeout: 300000 })
  await sleep(1000)
  const swapped = await page.evaluate(() => ({
    failed: !!document.querySelector('.rp-fail'),
    text: document.querySelector('.rp-fail')?.textContent ?? '',
    figures: document.querySelectorAll('canvas, img.rp-slice-img, img[data-figure], svg.rp-chart, .rp-sheet').length,
  }))
  check('内外侧互换的病例拒绝出报告', swapped.failed)
  check('并说明了拒绝原因', /swapped|互换/.test(swapped.text), swapped.text.slice(0, 90))
  check('拒绝时不画任何图形、不排任何页', swapped.figures === 0, `${swapped.figures}`)
} finally {
  await browser.close()
}

// The swapped-label knee answers 409 by design; Chrome logs that as an error.
const real = errors.filter((e) => !/favicon/i.test(e) && !/409 \(Conflict\)/.test(e))
check('无控制台错误', real.length === 0, real.slice(0, 2).join(' | '))
check('无外部请求（离线可用）', external.length === 0, external.slice(0, 3).join(' | '))

if (problems.length) {
  console.log(`\n${problems.length} problems:`)
  for (const p of problems) console.log('  - ' + p)
  process.exit(1)
}
console.log('\nALL REPORT FIGURE CHECKS PASSED')
