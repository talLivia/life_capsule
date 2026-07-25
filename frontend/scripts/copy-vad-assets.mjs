// Copies the Silero VAD model + ONNX runtime wasm out of node_modules and
// into public/vad, so they are SELF-HOSTED (no third-party CDN fetch on page
// load, and voice input works with no network in dev).
//
// Done at install/build time rather than committed: the files are ~15 MB of
// binaries, and copying them keeps them automatically in step with the
// installed package versions instead of drifting from them.
//
// Runs on postinstall AND prebuild — prebuild matters because a deploy that
// skips postinstall would otherwise ship without the assets, and the failure
// mode is a silently dead microphone.
import { copyFileSync, existsSync, mkdirSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

const root = join(dirname(fileURLToPath(import.meta.url)), '..')
const outDir = join(root, 'public', 'vad')

const ASSETS = [
  ['node_modules/@ricky0123/vad-web/dist/silero_vad_v5.onnx', 'silero_vad_v5.onnx'],
  ['node_modules/@ricky0123/vad-web/dist/vad.worklet.bundle.min.js', 'vad.worklet.bundle.min.js'],
  // Only the plain SIMD build. The .jsep (WebGPU) variant is another ~26 MB
  // and buys nothing for a model this small running single-threaded.
  ['node_modules/onnxruntime-web/dist/ort-wasm-simd-threaded.wasm', 'ort-wasm-simd-threaded.wasm'],
]

mkdirSync(outDir, { recursive: true })

let copied = 0
for (const [from, to] of ASSETS) {
  const src = join(root, from)
  if (!existsSync(src)) {
    console.error(`[vad-assets] MISSING ${from} — voice input will not work.`)
    process.exitCode = 1
    continue
  }
  copyFileSync(src, join(outDir, to))
  copied++
}
console.log(`[vad-assets] ${copied}/${ASSETS.length} asset(s) -> public/vad`)
