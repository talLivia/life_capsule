'use client'

/**
 * The photos under a /talk answer (docs/MEDIA_GALLERY.md §9.4): the union of
 * the galleries of every life period the clip's footage came from — one
 * gallery per category (§2.2), unioned when a turn drew on several, never
 * filtered further. Clicking opens the same PhotoLightbox the timeline uses.
 *
 * Photos are decoration on the answer, not part of it: while loading or when
 * the periods have no photos this renders NOTHING — no spinner, no empty
 * frame. The clip is the answer; the photos accompany it when they exist.
 */

import { useEffect, useState } from 'react'
import { api } from '@/lib/api'
import { PhotoLightbox } from '@/components/media/PhotoLightbox'
import type { MediaAsset } from '@/lib/types'

// One fetch per category per page load, shared across every turn in the
// conversation — three answers from ילדות must not fetch its gallery three
// times. A failed fetch caches [] (decoration never retries a failing call).
const galleryCache = new Map<string, Promise<MediaAsset[]>>()

function galleryFor(category: string): Promise<MediaAsset[]> {
  let cached = galleryCache.get(category)
  if (!cached) {
    cached = api.listMedia({ category }).catch(() => []) as Promise<MediaAsset[]>
    galleryCache.set(category, cached)
  }
  return cached
}

export function TurnPhotoGallery({ categories }: { categories: string[] }) {
  const [photos, setPhotos] = useState<MediaAsset[]>([])
  const [lightboxIndex, setLightboxIndex] = useState<number | null>(null)

  useEffect(() => {
    let cancelled = false
    Promise.all(categories.map(galleryFor)).then((galleries) => {
      if (cancelled) return
      // Union in category order (the order the answer plays its footage),
      // deduped by id — a photo cannot appear twice however the turn's
      // categories overlap.
      const seen = new Set<string>()
      const union: MediaAsset[] = []
      for (const gallery of galleries) {
        for (const photo of gallery) {
          if (!seen.has(photo.id)) {
            seen.add(photo.id)
            union.push(photo)
          }
        }
      }
      setPhotos(union)
    })
    return () => {
      cancelled = true
    }
  }, [categories])

  if (photos.length === 0) return null

  return (
    <>
      <ul className="flex flex-wrap gap-1.5">
        {photos.map((photo, i) => (
          <li key={photo.id}>
            <button
              type="button"
              onClick={() => setLightboxIndex(i)}
              aria-label={photo.caption || 'Open photo'}
              className="block w-16 h-16 rounded-lg overflow-hidden border border-calm-border dark:border-calm-borderDark hover:opacity-90 focus:outline-none focus:ring-2 focus:ring-calm-sage-500"
            >
              {/* eslint-disable-next-line @next/next/no-img-element */}
              <img
                src={photo.url}
                alt={photo.caption ?? ''}
                className="w-full h-full object-cover"
              />
            </button>
          </li>
        ))}
      </ul>
      {lightboxIndex !== null && (
        <PhotoLightbox
          photos={photos}
          initialIndex={lightboxIndex}
          onClose={() => setLightboxIndex(null)}
        />
      )}
    </>
  )
}
