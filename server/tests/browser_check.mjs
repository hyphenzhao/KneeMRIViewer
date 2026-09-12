/**
 * Headless browser smoke test - the half the API tests cannot reach.
 *
 * Drives a real Chromium (WebGL via SwiftShader), opens the first annotated
 * case, and asserts the four viewports actually draw something: each canvas
 * must be non-blank and, for the three MPR planes, must contain the segment
 * colours the label set declares.
 *
 *   node tests/browser_check.mjs [baseUrl] [outdir]
 */
import fs from 'node:fs/promises'
import path from 'node:path'

import puppeteer from 'puppeteer'

const BASE = process.argv[2] || process.env.MRIV_BASE || 'http://127.0.0.1:8080'
const OUT = process.argv[3] || 'browser-check'

const problems = []
const consoleErrors = []
const failedRequests = []
const externalRequests = []

function check(name, cond, detail = '') {
  if (cond) console.log(`  ok    ${name}${detail ? ' - ' + detail : ''}`)
  else {
    console.log(`  FAIL  ${name}${detail ? ' - ' + detail : ''}`)
    problems.push(name + (detail ? ' - ' + detail : ''))
  }
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms))

await fs.mkdir(OUT, { recursive: true })

const browser = await puppeteer.launch({
  executablePath: process.env.CHROME_PATH || undefined,
  headless: true,
  args: [
    '--no-sandbox',
    '--enable-unsafe-swiftshader', // software WebGL: no GPU in this environment
    '--use-gl=swiftshader',
    '--window-size=1600,1000',
  ],
})

try {
  const page = await browser.newPage()
  await page.setViewport({ width: 1600, height: 1000 })

  page.on('console', (m) => {
    // Warnings matter here: Cornerstone skips an incompatible segmentation
    // representation with a console.warn and no error, so an overlay can go
    // missing with nothing in the error stream at all.
    if (m.type() === 'error') consoleErrors.push(m.text().slice(0, 300))
    else if (m.type() === 'warning' && /segmentation|representation|Skipping/i.test(m.text())) {
      consoleErrors.push('warn: ' + m.text().slice(0, 300))
    }
  })
  page.on('pageerror', (e) => consoleErrors.push('pageerror: ' + String(e).slice(0, 300)))
  page.on('requestfailed', (r) =>
    failedRequests.push(`${r.failure()?.errorText} ${r.url().slice(0, 120)}`),
  )
  page.on('request', (r) => {
    const u = new URL(r.url())
    if (!['http:', 'https:'].includes(u.protocol)) return
    if (u.host !== new URL(BASE).host) externalRequests.push(r.url())
  })

  console.log('loading', BASE)
  await page.goto(BASE, { waitUntil: 'networkidle2', timeout: 60000 })

  const webgl = await page.evaluate(() => {
    const c = document.createElement('canvas')
    const gl = c.getContext('webgl2') || c.getContext('webgl')
    if (!gl) return null
    const dbg = gl.getExtension('WEBGL_debug_renderer_info')
    return dbg ? gl.getParameter(dbg.UNMASKED_RENDERER_WEBGL) : 'unknown'
  })
  check('WebGL available', !!webgl, String(webgl))

  await page.waitForSelector('.sidebar-head select', { timeout: 30000 })
  await page.select('.sidebar-head select', process.env.MRIV_DATASET || 'ds0826')
  await sleep(1500)
  await page.waitForSelector('.plist li', { timeout: 30000 })
  const n = await page.$$eval('.plist li', (els) => els.length)
  check('patient list rendered', n > 0, `${n} rows`)

  // Filter to annotated cases, then open the first one.
  await page.click('.sidebar-head input[type=checkbox]')
  await sleep(1500)
  await page.waitForSelector('.plist li', { timeout: 20000 })
  const annotated = await page.$$eval('.plist li', (els) => els.length)
  check('annotated filter works', annotated > 0, `${annotated} rows`)

  await page.click('.plist li')
  console.log('  opened first annotated case, waiting for volume + labelmap…')

  // Wait until every viewport canvas has actually drawn something.
  const drew = await page
    .waitForFunction(
      () => {
        const cs = [...document.querySelectorAll('.vp canvas')]
        if (cs.length < 4) return false
        return cs.every((c) => c.width > 10 && c.height > 10)
      },
      { timeout: 120000, polling: 500 },
    )
    .then(() => true)
    .catch(() => false)
  check('four viewport canvases exist and are sized', drew)

  // Let volume load, MPR render, meshes arrive.
  await sleep(20000)

  const stats = await page.evaluate(() => {
    const out = []
    for (const vp of document.querySelectorAll('.vp')) {
      const tag = vp.querySelector('.tag')?.textContent?.trim() ?? '?'
      const c = vp.querySelector('canvas')
      if (!c) {
        out.push({ tag, ok: false, reason: 'no canvas' })
        continue
      }
      // Cornerstone renders into one shared offscreen WebGL canvas and blits
      // each viewport's region onto a plain 2D canvas, so read pixels from 2D.
      let pixels = null
      try {
        const ctx = c.getContext('2d', { willReadFrequently: true })
        if (!ctx) throw new Error('no 2d context')
        pixels = ctx.getImageData(0, 0, c.width, c.height).data
      } catch (e) {
        out.push({ tag, ok: false, reason: 'getImageData failed: ' + e })
        continue
      }
      let nonBlack = 0
      let coloured = 0
      let total = 0
      const seen = new Set()
      for (let i = 0; i < pixels.length; i += 4 * 97) {
        const r = pixels[i]
        const g = pixels[i + 1]
        const b = pixels[i + 2]
        total++
        if (r + g + b > 24) nonBlack++
        const mx = Math.max(r, g, b)
        const mn = Math.min(r, g, b)
        if (mx > 60 && mx - mn > 40) coloured++
        seen.add(`${r >> 4},${g >> 4},${b >> 4}`)
      }
      out.push({
        tag,
        ok: true,
        size: `${c.width}x${c.height}`,
        sampled: total,
        nonBlackPct: Math.round((100 * nonBlack) / total),
        // One decimal: only the cartilage is shown by default, and on a
        // 3 mm-slice MPR that is around 1% of the pixels.
        colouredPct: Math.round((1000 * coloured) / total) / 10,
        distinctColours: seen.size,
      })
    }
    return out
  })

  console.log('\n  viewport pixel analysis:')
  for (const s of stats) {
    if (!s.ok) {
      console.log(`    ${s.tag}: ${s.reason}`)
      continue
    }
    console.log(
      `    ${s.tag.padEnd(26)} ${s.size.padEnd(11)} non-black ${String(s.nonBlackPct).padStart(3)}%` +
        `  coloured ${String(s.colouredPct).padStart(3)}%  distinct ${s.distinctColours}`,
    )
  }

  const mpr = stats.filter((s) => s.ok && /Axial|Coronal|Sagittal/.test(s.tag))
  const v3d = stats.find((s) => s.ok && /3D/.test(s.tag))

  check('3 MPR viewports analysed', mpr.length === 3, `${mpr.length}`)
  for (const s of mpr) {
    check(`${s.tag}: drew image content`, s.nonBlackPct >= 5, `${s.nonBlackPct}% non-black`)
    check(`${s.tag}: segmentation overlay visible`, s.colouredPct >= 0.3,
      `${s.colouredPct}% saturated pixels`)
  }
  if (v3d) {
    check('3D viewport drew surfaces', v3d.colouredPct >= 1,
      `${v3d.colouredPct}% saturated pixels`)
  }

  // Label panel populated from the segmentation.
  const labels = await page.$$eval('.labels li', (els) =>
    els.map((e) => e.querySelector('.nm input')?.value ?? ''),
  )
  check('label panel populated', labels.length >= 8, `${labels.length}: ${labels.join(' / ')}`)

  await page.screenshot({ path: path.join(OUT, 'viewer.png'), fullPage: false })

  // Toggle a label off and confirm the picture changes.
  const before = await page.screenshot({ encoding: 'base64' })
  await page.click('.labels li input[type=checkbox]')
  await sleep(3000)
  const after = await page.screenshot({ encoding: 'base64' })
  check('toggling a label changes the render', before !== after)
  await page.screenshot({ path: path.join(OUT, 'viewer-label-off.png') })

  check('no requests to external hosts', externalRequests.length === 0,
    externalRequests.slice(0, 3).join(', '))
  const realFailures = failedRequests.filter((f) => !/favicon/i.test(f))
  check('no failed requests', realFailures.length === 0, realFailures.slice(0, 3).join('; '))
  const realErrors = consoleErrors.filter((e) => !/favicon|Download the React/i.test(e))
  check('no console errors', realErrors.length === 0, realErrors.slice(0, 2).join(' | '))

  if (realErrors.length) {
    console.log('\n  console errors:')
    for (const e of realErrors.slice(0, 10)) console.log('    ' + e)
  }
} finally {
  await browser.close()
}

console.log(`\nscreenshots in ${path.resolve(OUT)}`)
if (problems.length) {
  console.log(`\n${problems.length} problems:`)
  for (const p of problems) console.log('  - ' + p)
  process.exit(1)
}
console.log('\nALL BROWSER CHECKS PASSED')
