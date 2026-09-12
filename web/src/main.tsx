import { useEffect, useState } from 'react'
import { createRoot } from 'react-dom/client'

import App from './App'
import ReportPage from './pages/ReportPage'
import './styles.css'

/**
 * Hash routing, deliberately.
 *
 * Vite builds with `base: './'` and the backend serves the bundle through
 * StaticFiles with no SPA fallback, so a real path like /report/8 would 404 on
 * reload. A hash keeps the report shareable as a plain link.
 *
 * `#/metrics/<id>` was the old interactive dashboard; its figures now live in
 * the report itself, so old links are redirected rather than broken.
 */
function normalise(hash: string): string {
  const legacy = /^#\/metrics\/(\d+)/.exec(hash)
  if (!legacy) return hash
  const to = `#/report/${legacy[1]}`
  // replace(), not assign(): the old URL must not stay in history, or Back
  // would bounce between the two forever. Fragment-only, so no reload.
  window.location.replace(to)
  return to
}

function Router() {
  const [hash, setHash] = useState(() => normalise(window.location.hash))
  useEffect(() => {
    const on = () => setHash(normalise(window.location.hash))
    window.addEventListener('hashchange', on)
    return () => window.removeEventListener('hashchange', on)
  }, [])
  if (hash.startsWith('#/report/')) return <ReportPage />
  return <App />
}

// No StrictMode double-invoke here: Cornerstone's rendering engine and tool
// groups are global singletons keyed by id, and a synthetic remount tears down
// WebGL contexts that the viewport is still holding.
createRoot(document.getElementById('root')!).render(<Router />)
