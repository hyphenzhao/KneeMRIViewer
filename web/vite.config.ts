import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// Cornerstone3D 5.x is ESM-only and ships an ES worker. We deliberately do NOT
// use @cornerstonejs/dicom-image-loader (the backend hands us decoded volumes),
// so none of the usual dicom-parser / WASM-codec bundler workarounds apply and
// the build has zero WASM and zero external hosts.
export default defineConfig({
  plugins: [react()],
  base: './',
  resolve: {
    alias: {
      // vtk.js pulls in xmlbuilder2 for its XML writers, and that extends
      // Node's EventEmitter. Vite externalises node builtins for the browser,
      // so the class ends up extending `undefined` and the whole bundle dies
      // at module-evaluation time with "Class extends value undefined" - before
      // React ever mounts. The trailing slash forces resolution to the npm
      // polyfill package instead of the builtin.
      events: 'events/',
      url: 'url/',
    },
  },
  worker: { format: 'es' },
  build: { outDir: 'dist', target: 'es2022', sourcemap: false, chunkSizeWarningLimit: 4096 },
  server: {
    host: '0.0.0.0',
    // MRIV_API lets `npm run dev` point at the real server on the LAN.
    proxy: {
      '/api': {
        target: process.env.MRIV_API || 'http://127.0.0.1:8080',
        changeOrigin: true,
      },
    },
  },
})
