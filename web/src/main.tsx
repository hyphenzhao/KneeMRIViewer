import { useEffect, useState } from 'react'
import { createRoot } from 'react-dom/client'

import App from './App'
import MetricsPage from './pages/MetricsPage'
import './styles.css'

/**
 * Hash routing, deliberately.
 *
 * Vite builds with `base: './'` and the backend serves the bundle through
 * StaticFiles with no SPA fallback, so a real path like /metrics/8 would 404 on
 * reload. A hash keeps the metrics dashboard shareable as a plain link - which
 * is the whole point of it being a separate page.
 */
function Router() {
  const [hash, setHash] = useState(window.location.hash)
  useEffect(() => {
    const on = () => setHash(window.location.hash)
    window.addEventListener('hashchange', on)
    return () => window.removeEventListener('hashchange', on)
  }, [])
  return hash.startsWith('#/metrics/') ? <MetricsPage /> : <App />
}

// No StrictMode double-invoke here: Cornerstone's rendering engine and tool
// groups are global singletons keyed by id, and a synthetic remount tears down
// WebGL contexts that the viewport is still holding.
createRoot(document.getElementById('root')!).render(<Router />)
