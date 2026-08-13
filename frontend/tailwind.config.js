/** @type {import('tailwindcss').Config} */
module.exports = {
  content: [
    './pages/**/*.{js,ts,jsx,tsx,mdx}',
    './components/**/*.{js,ts,jsx,tsx,mdx}',
    './app/**/*.{js,ts,jsx,tsx,mdx}',
    './store/**/*.{js,ts,jsx,tsx}',
  ],
  darkMode: 'class',
  theme: {
    extend: {
      colors: {
        // Every scale below reads CSS variables (RGB triples) defined in
        // globals.css — :root carries the dark values (the app's default),
        // html.light restates them with the green/cream palette. Components
        // reference tokens, never a theme's literal hex, so the two themes
        // cannot drift (docs/LIGHT_MODE notes in globals.css).
        primary: {
          50: 'rgb(var(--primary-50) / <alpha-value>)',
          100: 'rgb(var(--primary-100) / <alpha-value>)',
          200: 'rgb(var(--primary-200) / <alpha-value>)',
          300: 'rgb(var(--primary-300) / <alpha-value>)',
          400: 'rgb(var(--primary-400) / <alpha-value>)',
          500: 'rgb(var(--primary-500) / <alpha-value>)',
          600: 'rgb(var(--primary-600) / <alpha-value>)',
          700: 'rgb(var(--primary-700) / <alpha-value>)',
          800: 'rgb(var(--primary-800) / <alpha-value>)',
          900: 'rgb(var(--primary-900) / <alpha-value>)',
        },
        accent: {
          50: 'rgb(var(--accent-50) / <alpha-value>)',
          100: 'rgb(var(--accent-100) / <alpha-value>)',
          200: 'rgb(var(--accent-200) / <alpha-value>)',
          300: 'rgb(var(--accent-300) / <alpha-value>)',
          400: 'rgb(var(--accent-400) / <alpha-value>)',
          500: 'rgb(var(--accent-500) / <alpha-value>)',
          600: 'rgb(var(--accent-600) / <alpha-value>)',
          700: 'rgb(var(--accent-700) / <alpha-value>)',
          800: 'rgb(var(--accent-800) / <alpha-value>)',
          900: 'rgb(var(--accent-900) / <alpha-value>)',
        },
        surface: {
          950: 'rgb(var(--surface-950) / <alpha-value>)',
          900: 'rgb(var(--surface-900) / <alpha-value>)',
          800: 'rgb(var(--surface-800) / <alpha-value>)',
          700: 'rgb(var(--surface-700) / <alpha-value>)',
          600: 'rgb(var(--surface-600) / <alpha-value>)',
          500: 'rgb(var(--surface-500) / <alpha-value>)',
        },
        // Semantic text/border/overlay tokens — what components use instead
        // of text-white / text-gray-N / border-white/N / bg-white/N.
        ink: 'rgb(var(--ink) / <alpha-value>)',
        'ink-soft': 'rgb(var(--ink-soft) / <alpha-value>)',
        muted: 'rgb(var(--muted) / <alpha-value>)',
        muted2: 'rgb(var(--muted2) / <alpha-value>)',
        edge: 'var(--edge)',
        'edge-strong': 'var(--edge-strong)',
        veil: 'var(--veil)',
        navbar: 'var(--nav-bg)',
        navpill: 'var(--nav-pill)',
        navfg: 'var(--nav-fg)',
        'navfg-strong': 'var(--nav-fg-strong)',
        neon: {
          purple: '#a855f7',
          blue: '#3b82f6',
          cyan: '#06b6d4',
          pink: '#ec4899',
        },
        // Calm palette — used only by /record (the guided-interview
        // recording flow). Deliberately un-neon: the storyteller may be
        // recording emotionally difficult content, so this page trades the
        // rest of the app's glowing dark theme for a quiet, warm, low-
        // contrast one instead.
        calm: {
          paper: '#f7f4ee',
          card: '#fffdf9',
          ink: '#2f3430',
          inkmuted: '#6b6f68',
          border: '#e5e0d4',
          sage: {
            50: '#f1f5f1',
            100: '#dfe8e0',
            300: '#a9c2ac',
            500: '#6f8f74',
            600: '#5a7a5f',
            700: '#48624c',
          },
          paperDark: '#161b17',
          cardDark: '#1d2420',
          inkDark: '#e8ece8',
          inkmutedDark: '#9aa39c',
          borderDark: '#2a332c',
        },
      },
      backgroundImage: {
        'gradient-radial': 'radial-gradient(var(--tw-gradient-stops))',
        'gradient-conic': 'conic-gradient(from 180deg at 50% 50%, var(--tw-gradient-stops))',
        'hero-gradient': 'linear-gradient(135deg, #0a0a0f 0%, #1a0533 50%, #0a0a0f 100%)',
        'card-gradient': 'var(--card-gradient)',
        'glow-gradient': 'radial-gradient(ellipse at center, rgba(168,85,247,0.15) 0%, transparent 70%)',
      },
      animation: {
        'pulse-slow': 'pulse 3s cubic-bezier(0.4, 0, 0.6, 1) infinite',
        'float': 'float 6s ease-in-out infinite',
        'glow': 'glow 2s ease-in-out infinite alternate',
        'wave': 'wave 1.5s ease-in-out infinite',
        'shimmer': 'shimmer 2s linear infinite',
        'typewriter': 'typewriter 0.05s steps(1) forwards',
        'fade-in': 'fadeIn 0.5s ease-out forwards',
        'slide-up': 'slideUp 0.4s ease-out forwards',
        'slide-in-right': 'slideInRight 0.4s ease-out forwards',
        'scale-in': 'scaleIn 0.3s ease-out forwards',
        'spin-slow': 'spin 8s linear infinite',
        'ping-slow': 'ping 2s cubic-bezier(0, 0, 0.2, 1) infinite',
        'waveform': 'waveform 1.2s ease-in-out infinite',
        'aurora': 'aurora 8s ease infinite',
      },
      keyframes: {
        float: {
          '0%, 100%': { transform: 'translateY(0px)' },
          '50%': { transform: 'translateY(-20px)' },
        },
        glow: {
          '0%': { boxShadow: '0 0 20px rgba(168,85,247,0.3)' },
          '100%': { boxShadow: '0 0 40px rgba(168,85,247,0.8), 0 0 80px rgba(59,130,246,0.3)' },
        },
        shimmer: {
          '0%': { backgroundPosition: '-1000px 0' },
          '100%': { backgroundPosition: '1000px 0' },
        },
        fadeIn: {
          '0%': { opacity: '0' },
          '100%': { opacity: '1' },
        },
        slideUp: {
          '0%': { transform: 'translateY(20px)', opacity: '0' },
          '100%': { transform: 'translateY(0)', opacity: '1' },
        },
        slideInRight: {
          '0%': { transform: 'translateX(20px)', opacity: '0' },
          '100%': { transform: 'translateX(0)', opacity: '1' },
        },
        scaleIn: {
          '0%': { transform: 'scale(0.9)', opacity: '0' },
          '100%': { transform: 'scale(1)', opacity: '1' },
        },
        waveform: {
          '0%, 100%': { height: '4px' },
          '50%': { height: '24px' },
        },
        aurora: {
          '0%, 100%': { backgroundPosition: '0% 50%' },
          '50%': { backgroundPosition: '100% 50%' },
        },
      },
      backdropBlur: {
        xs: '2px',
      },
      boxShadow: {
        'glow-sm': '0 0 10px var(--glow-primary-weak)',
        'glow': '0 0 20px var(--glow-primary)',
        'glow-lg': '0 0 40px var(--glow-primary)',
        'glow-blue': '0 0 20px var(--glow-accent)',
        'inner-glow': 'inset 0 0 20px var(--glow-primary-faint)',
        'glass': 'var(--shadow-glass)',
        'card': 'var(--shadow-card)',
        'neon-purple': '0 0 5px #a855f7, 0 0 20px #a855f7, 0 0 40px #a855f7',
        'neon-blue': '0 0 5px #3b82f6, 0 0 20px #3b82f6, 0 0 40px #3b82f6',
      },
      fontFamily: {
        sans: ['Inter', 'system-ui', 'sans-serif'],
        mono: ['JetBrains Mono', 'monospace'],
      },
      transitionTimingFunction: {
        'spring': 'cubic-bezier(0.175, 0.885, 0.32, 1.275)',
      },
    },
  },
  plugins: [],
};
