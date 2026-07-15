// `next lint` was removed in Next.js 16 — we run ESLint 9 directly with a
// flat config. eslint-config-next@16 ships a native flat-config array
// (core-web-vitals + typescript), so we spread it directly; no FlatCompat
// shim is needed (and FlatCompat actually breaks on the plugin objects).
import next from 'eslint-config-next'

const eslintConfig = [
  ...next,
  {
    ignores: ['.next/**', 'node_modules/**', 'next-env.d.ts'],
  },
  {
    rules: {
      // The chat pipeline intentionally reads refs in effects with curated
      // dependency arrays (documented inline). The exhaustive-deps autofix
      // would break the WebSocket lifecycle, so keep it advisory.
      'react-hooks/exhaustive-deps': 'warn',

      // eslint-plugin-react-hooks v6 (bundled by Next 16) ships the React
      // Compiler ruleset. This project does NOT enable React Compiler
      // (no `reactCompiler` in next.config, no babel plugin), so these
      // advisory rules flag normal, correct React — ref mutation in
      // callbacks, event-handler assignment, calling a setState updater
      // helper. Turn them off until/unless the compiler is adopted.
      // `react-hooks/purity` is deliberately left ON — Math.random/Date.now
      // in render is a real determinism bug worth catching.
      'react-hooks/immutability': 'off',
      'react-hooks/preserve-manual-memoization': 'off',
      // The mount effect adopts/creates the chat session and sets state once —
      // a single extra render on mount, not the cascading-render problem this
      // compiler rule targets. Off, consistent with the rules above.
      'react-hooks/set-state-in-effect': 'off',
    },
  },
]

export default eslintConfig
