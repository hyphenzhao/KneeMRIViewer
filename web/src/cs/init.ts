/**
 * Cornerstone3D bootstrap.
 *
 * Note what is deliberately absent: `@cornerstonejs/dicom-image-loader`. The
 * backend converts each series to a canonical volume once, so the browser never
 * parses DICOM. That removes the JPEG/JPEG2000 WASM codecs, `dicom-parser` and
 * its CommonJS bundler workaround - the production build ends up pure JS with
 * no external hosts, which is what makes the air-gapped install trivial.
 */
import { init as coreInit, imageLoader, volumeLoader } from '@cornerstonejs/core'
import { init as toolsInit } from '@cornerstonejs/tools'

import { MRIV_SCHEME, mrivImageLoader, mrivVolumeLoader } from './volumeLoader'

/**
 * A do-nothing PolySeg add-on.
 *
 * PolySeg converts between segmentation representations (labelmap <-> surface
 * <-> contour). We never need that: the server precomputes the surfaces and
 * ships them alongside the labelmap. But Cornerstone's segmentation event
 * manager calls `getUpdateFunction(viewport)` unconditionally, and the surface
 * display's implementation is `polySeg.updateSurfaceData(...)` with no null
 * check - so with no add-on registered it throws
 * "Cannot read properties of null (reading 'updateSurfaceData')" out of an
 * event handler on every segmentation update.
 *
 * Registering this stub silences that cleanly. The real add-on would drag
 * @icr/polyseg-wasm into the bundle, which is exactly the WASM dependency the
 * air-gapped deployment is built to avoid.
 */
const noopPolySeg = {
  canComputeRequestedRepresentation: () => false,
  computeSurfaceData: async () => undefined,
  computeLabelmapData: async () => undefined,
  computeContourData: async () => undefined,
  updateSurfaceData: async () => undefined,
  updateLabelmapData: async () => undefined,
  updateContourData: async () => undefined,
  init: () => undefined,
}

let ready: Promise<void> | null = null

export function initCornerstone(): Promise<void> {
  if (!ready) {
    ready = (async () => {
      await coreInit()
      await toolsInit({ addons: { polySeg: noopPolySeg } } as never)
      volumeLoader.registerVolumeLoader(MRIV_SCHEME, mrivVolumeLoader)
      imageLoader.registerImageLoader(MRIV_SCHEME, mrivImageLoader)
    })()
  }
  return ready
}
