/**
 * Switching between cases.
 *
 * Two regressions this guards:
 *
 * 1. Reopening a case you already looked at threw
 *    "Segmentation with id mriv:seg:N already exists" and left the viewer with
 *    no labels at all.
 * 2. Opening a series with *no* segmentation kept showing the previous
 *    patient's cartilage - both the 2D overlay and the 3D surfaces. In a viewer
 *    whose whole job is checking segmentation output, that is a wrong answer,
 *    not a cosmetic glitch.
 *
 * Everything is measured from pixels and the DOM, so it runs against the
 * production build (which has no debug hooks).
 *
 *   node tests/browser_switch_check.mjs [baseUrl]
 */
import puppeteer from 'puppeteer'

const BASE = process.argv[2] || process.env.MRIV_BASE || 'http://127.0.0.1:8080'
const DATASET = process.env.MRIV_DATASET || 'ds0826'
const sleep = (ms) => new Promise((r) => setTimeout(r, ms))

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

try {
  const page = await browser.newPage()
  await page.setViewport({ width: 1600, height: 1000 })
  page.on('pageerror', (e) => errors.push(String(e).slice(0, 200)))
  page.on('console', (m) => {
    if (m.type() === 'error') errors.push(m.text().slice(0, 200))
  })

  await page.goto(BASE, { waitUntil: 'networkidle2', timeout: 60000 })
  await page.waitForSelector('.sidebar-head select', { timeout: 30000 })
  await page.select('.sidebar-head select', DATASET)
  await sleep(1500)

  /** Colour saturation per viewport - the signature of a segmentation overlay. */
  const measure = () =>
    page.evaluate(() => {
      const out = {}
      for (const vp of document.querySelectorAll('.vp')) {
        const tag = vp.querySelector('.tag')?.textContent?.trim() ?? '?'
        const c = vp.querySelector('canvas')
        if (!c) continue
        const ctx = c.getContext('2d', { willReadFrequently: true })
        const px = ctx.getImageData(0, 0, c.width, c.height).data
        let coloured = 0
        let nonBlack = 0
        let total = 0
        for (let i = 0; i < px.length; i += 4 * 97) {
          const r = px[i]
          const g = px[i + 1]
          const b = px[i + 2]
          total++
          if (r + g + b > 24) nonBlack++
          const mx = Math.max(r, g, b)
          if (mx > 60 && mx - Math.min(r, g, b) > 40) coloured++
        }
        const key = /3D/.test(tag)
          ? 'v3d'
          : /Sagittal/.test(tag)
            ? 'sag'
            : /Coronal/.test(tag)
              ? 'cor'
              : /Axial/.test(tag)
                ? 'ax'
                : null
        if (key) out[key] = {
          coloured: Math.round((100 * coloured) / total),
          nonBlack: Math.round((100 * nonBlack) / total),
        }
      }
      out.labels = document.querySelectorAll('.labels li').length
      out.err = document.querySelector('.err')?.textContent?.trim() ?? ''
      return out
    })

  const openRow = async (index, waitMs = 22000) => {
    const rows = await page.$$('.plist li')
    if (!rows[index]) throw new Error(`no patient row ${index}`)
    await rows[index].click()
    await sleep(waitMs)
    return measure()
  }

  // ---- annotated -> annotated -> back to the first -------------------
  await page.click('.sidebar-head input[type=checkbox]')   // only annotated
  await sleep(1500)

  const PLANES = ['ax', 'cor', 'sag']
  const planeName = { ax: 'axial', cor: 'coronal', sag: 'sagittal' }

  const first = await openRow(0)
  check('case A: labels listed', first.labels >= 8, `${first.labels}`)
  for (const k of PLANES) {
    // Every plane, not just the acquisition one. A blank axial reformat hid
    // here for a while precisely because only sagittal was being checked.
    check(`case A: ${planeName[k]} shows image`, first[k].nonBlack >= 5,
      `${first[k].nonBlack}% non-black`)
    check(`case A: ${planeName[k]} shows overlay`, first[k].coloured >= 1,
      `${first[k].coloured}%`)
  }
  check('case A: 3D surfaces', first.v3d.coloured >= 5, `${first.v3d.coloured}%`)

  const second = await openRow(1)
  check('case B: labels listed', second.labels >= 8, `${second.labels}`)
  check('case B: 3D surfaces', second.v3d.coloured >= 5, `${second.v3d.coloured}%`)

  const back = await openRow(0)
  check('reopening case A raises no error', back.err === '', back.err.slice(0, 120))
  check('reopening case A restores labels', back.labels >= 8, `${back.labels}`)
  for (const k of PLANES) {
    check(`reopening case A: ${planeName[k]} shows image`, back[k].nonBlack >= 5,
      `${back[k].nonBlack}%`)
    check(`reopening case A: ${planeName[k]} shows overlay`, back[k].coloured >= 1,
      `${back[k].coloured}%`)
  }
  check('reopening case A restores 3D', back.v3d.coloured >= 5, `${back.v3d.coloured}%`)
  check(
    'no "already exists" error anywhere',
    !errors.some((e) => /already exists/i.test(e)),
    errors.filter((e) => /already exists/i.test(e))[0] ?? '',
  )

  // ---- annotated -> UNannotated: everything must clear ----------------
  await page.click('.sidebar-head input[type=checkbox]')   // show all again
  await sleep(2000)

  const unannotatedIndex = await page.$$eval('.plist li', (els) =>
    els.findIndex((e) => /无标注/.test(e.querySelector('.badge')?.textContent ?? '')),
  )
  if (unannotatedIndex < 0) {
    check('an unannotated case exists to switch to', false)
  } else {
    const plain = await openRow(unannotatedIndex)
    check('unannotated: no label rows', plain.labels === 0, `${plain.labels} rows`)
    for (const k of PLANES) {
      check(`unannotated: ${planeName[k]} shows image`, plain[k].nonBlack >= 5,
        `${plain[k].nonBlack}% non-black`)
      check(`unannotated: ${planeName[k]} overlay cleared`, plain[k].coloured < 1,
        `${plain[k].coloured}% coloured (previous case leaking through?)`)
    }
    check(
      'unannotated: 3D viewport cleared',
      plain.v3d.coloured < 1,
      `${plain.v3d.coloured}% coloured (previous case's surfaces still shown?)`,
    )
    check('unannotated: no error', plain.err === '', plain.err.slice(0, 120))
  }
} finally {
  await browser.close()
}

const real = errors.filter((e) => !/favicon/i.test(e))
check('no console errors', real.length === 0, real.slice(0, 2).join(' | '))

if (problems.length) {
  console.log(`\n${problems.length} problems:`)
  for (const p of problems) console.log('  - ' + p)
  process.exit(1)
}
console.log('\nALL SWITCHING CHECKS PASSED')
