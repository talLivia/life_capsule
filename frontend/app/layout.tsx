import './globals.css'
import type { Metadata } from 'next'
import { Inter } from 'next/font/google'
import { Toaster } from 'react-hot-toast'
import { PendingConfirmationsProvider } from '@/components/providers/PendingConfirmationsProvider'
import { QueryProvider } from '@/components/providers/QueryProvider'
import { StoreHydration } from '@/components/providers/StoreHydration'
import { ErrorBoundary } from '@/components/ui/ErrorBoundary'

const inter = Inter({ subsets: ['latin'] })

const SITE_URL = process.env.NEXT_PUBLIC_SITE_URL || 'http://localhost:3000'
const SITE_TITLE = 'Life Capsule — your family’s story, in their own voice'
const SITE_DESCRIPTION =
  'Life Capsule interviews a parent or grandparent question by question, and keeps ' +
  'their answers as they recorded them. When the family asks about a person, a place ' +
  'or a year later on, the answer is the storyteller’s own footage — never a ' +
  'synthetic voice speaking for them.'

export const metadata: Metadata = {
  metadataBase: new URL(SITE_URL),
  title: {
    default: SITE_TITLE,
    template: '%s · Life Capsule',
  },
  description: SITE_DESCRIPTION,
  applicationName: 'Life Capsule',
  keywords: [
    'life story', 'family history', 'oral history', 'guided interview',
    'legacy video', 'memoir', 'family archive', 'record grandparents',
    'personal history', 'storytelling', 'keepsake', 'genealogy',
  ],
  alternates: { canonical: '/' },
  openGraph: {
    type: 'website',
    url: SITE_URL,
    siteName: 'Life Capsule',
    title: SITE_TITLE,
    description: SITE_DESCRIPTION,
    locale: 'en_US',
  },
  twitter: {
    card: 'summary_large_image',
    title: SITE_TITLE,
    description: SITE_DESCRIPTION,
  },
  robots: {
    index: true,
    follow: true,
    googleBot: { index: true, follow: true, 'max-image-preview': 'large' },
  },
}

// Structured data (schema.org SoftwareApplication) — lets search engines show
// a rich result card instead of a bare blue link.
const JSON_LD = {
  '@context': 'https://schema.org',
  '@type': 'SoftwareApplication',
  name: 'Life Capsule',
  applicationCategory: 'LifestyleApplication',
  operatingSystem: 'Web',
  description: SITE_DESCRIPTION,
  url: SITE_URL,
  // The upstream avatar repo's links are deliberately NOT carried over: they
  // point at a different product, and `sameAs` is an identity claim.
  featureList: [
    'A guided interview of 129 questions across 16 chapters of a life',
    'Record answers as video, one question at a time, at your own pace',
    'Answers about a person, place or year come back as the storyteller’s own footage',
    'A family tree and timeline built from what was actually said',
    'Relatives can ask the archive questions in their own words',
  ],
}

export default function RootLayout({
  children,
}: {
  children: React.ReactNode
}) {
  return (
    <html lang="en" suppressHydrationWarning>
      <head>
        <script
          type="application/ld+json"
          dangerouslySetInnerHTML={{ __html: JSON.stringify(JSON_LD) }}
        />
      </head>
      <body className={inter.className}>
        {/* Skip link — appears only on keyboard focus, lets users bypass the nav */}
        <a
          href="#main-content"
          className="sr-only focus:not-sr-only focus:fixed focus:top-3 focus:left-3 focus:z-[100]
                     focus:px-4 focus:py-2 focus:rounded-lg focus:bg-primary-600 focus:text-white
                     focus:shadow-glow focus:outline-none focus:ring-2 focus:ring-primary-300"
        >
          Skip to main content
        </a>
        <StoreHydration />
        <QueryProvider>
        <PendingConfirmationsProvider>
          <ErrorBoundary>
            <div id="main-content">{children}</div>
          </ErrorBoundary>
          <Toaster
            position="top-right"
            toastOptions={{
              className: 'dark:bg-gray-800 dark:text-gray-100',
            }}
          />
        </PendingConfirmationsProvider>
        </QueryProvider>
      </body>
    </html>
  )
}
