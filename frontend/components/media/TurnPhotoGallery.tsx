'use client'

/**
 * The photos under a /talk answer (docs/MEDIA_GALLERY.md §9.4): the union of
 * the galleries of every life period the clip's footage came from — one
 * gallery per category (§2.2), unioned when a turn drew on several, never
 * filtered further.
 *
 * Presented as a STACKED POLAROID DECK (producer decision 2026-08-11), after
 * css-tricks.com/css-infinite-slider-flipping-through-polaroid-images: cards
 * stacked via grid-area 1/1, thick white polaroid frames, soft drop shadow,
 * and the front card sliding out to rejoin the back every few seconds so
 * every photo is seen with no interaction. The reference's pure-CSS keyframes
 * are per-N and ours is dynamic, so a small timer drives the same motion.
 * Clicking the deck still opens the shared PhotoLightbox at the photo
 * currently showing. Cycling pauses on hover, while the lightbox is open,
 * and entirely under prefers-reduced-motion.
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

/** How long each photo holds the front before sliding to the back. */
const CYCLE_MS = 3500
/** The slide-out itself — must match the inline transition duration. */
const SLIDE_MS = 700
/** Static per-card tilts, repeating — what makes the stack read as a real
 *  pile rather than perfectly registered prints. */
const TILTS = [-3, 2.5, -1.5, 3.5, -2.5, 1.5]

export function TurnPhotoGallery({
  categories,
  variant = 'calm',
}: {
  categories: string[]
  /** Which design system the deck sits in: 'calm' for the family /talk
   *  screen, 'app' for the producer's dark chat screen. The frames keep the
   *  same soft-gray polaroid color in both — only the shadow adapts. */
  variant?: 'calm' | 'app'
}) {
  const [photos, setPhotos] = useState<MediaAsset[]>([])
  const [front, setFront] = useState(0)
  // The card currently sliding out — kept on top while it travels, snapped
  // (transition-less) to the back of the pile once `front` advances.
  const [leaving, setLeaving] = useState<number | null>(null)
  const [paused, setPaused] = useState(false)
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
      setFront(0)
      setLeaving(null)
    })
    return () => {
      cancelled = true
    }
  }, [categories])

  // Hold each front card for CYCLE_MS, then start its slide. Restarts
  // whenever the front settles or a pause condition lifts.
  useEffect(() => {
    if (photos.length < 2 || paused || lightboxIndex !== null || leaving !== null) return
    if (window.matchMedia?.('(prefers-reduced-motion: reduce)').matches) return
    const hold = setTimeout(() => setLeaving(front), CYCLE_MS)
    return () => clearTimeout(hold)
  }, [photos.length, front, paused, lightboxIndex, leaving])

  // When the slide finishes, the traveller becomes the back card and the
  // next photo takes the front.
  useEffect(() => {
    if (leaving === null) return
    const slide = setTimeout(() => {
      setFront((leaving + 1) % photos.length)
      setLeaving(null)
    }, SLIDE_MS)
    return () => clearTimeout(slide)
  }, [leaving, photos.length])

  if (photos.length === 0) return null

  const n = photos.length
  const shadow =
    variant === 'calm'
      ? '0 6px 16px rgba(60, 50, 30, 0.25)'
      : '0 6px 16px rgba(0, 0, 0, 0.45)'
  const frontPhoto = photos[front]

  return (
    <div className="flex justify-center w-full">
      <button
        type="button"
        onClick={() => setLightboxIndex(front)}
        onPointerEnter={() => setPaused(true)}
        onPointerLeave={() => setPaused(false)}
        aria-label={
          frontPhoto.caption
            ? `Open photos — showing: ${frontPhoto.caption}`
            : `Open photos (${n})`
        }
        // p-6: a 328px-wide card tilted 3.5° overhangs its box by ~20px —
        // any less and the pile's corners clip on the overflow-hidden edge.
        className="grid place-items-center p-6 overflow-hidden cursor-pointer focus:outline-none focus-visible:ring-2 focus-visible:ring-primary-400 rounded-xl"
      >
        {photos.map((photo, i) => {
          const depth = (i - front + n) % n
          const isLeaving = leaving === i
          return (
            <figure
              key={photo.id}
              style={{
                gridArea: '1 / 1',
                // The traveller rides ABOVE the stack while it slides, then
                // re-sorts to the bottom the instant `front` advances — with
                // no transition on that snap, and opacity 0 at that moment,
                // so the jump to the back is never seen.
                zIndex: isLeaving ? n + 1 : n - depth,
                transform: isLeaving
                  ? 'translateX(130%) rotate(14deg)'
                  : `rotate(${TILTS[i % TILTS.length]}deg)`,
                opacity: isLeaving ? 0 : 1,
                transition: isLeaving
                  ? `transform ${SLIDE_MS}ms ease-in, opacity ${SLIDE_MS}ms ease-in`
                  : 'none',
                boxShadow: shadow,
              }}
              // #e0e0e0 (rgb 224 224 224) rather than pure white — the true-
              // white frame read too harsh against both themes.
              className="relative bg-[#e0e0e0] p-2 pb-7 m-0"
            >
              {/* 3:2 landscape — the classic photo-print proportion, which
                  reads as a real print where a square read as a thumbnail.
                  object-cover: the frame's shape is the polaroid's, never
                  the file's. */}
              {/* eslint-disable-next-line @next/next/no-img-element */}
              <img
                src={photo.url}
                alt={photo.caption ?? ''}
                className="w-[19.5rem] h-52 object-cover block"
                draggable={false}
              />
              {/* The polaroid's bottom margin is where a caption lives when
                  one exists — absolutely positioned INSIDE that margin so a
                  captioned card is exactly the height of an uncaptioned one
                  and the pile stays registered. leading-7 centers the line
                  in the pb-7 band. */}
              {photo.caption && (
                <figcaption
                  dir="auto"
                  className="absolute bottom-0 left-2 right-2 text-center text-[10px] leading-7 text-gray-500 truncate"
                >
                  {photo.caption}
                </figcaption>
              )}
            </figure>
          )
        })}
      </button>
      {lightboxIndex !== null && (
        <PhotoLightbox
          photos={photos}
          initialIndex={lightboxIndex}
          onClose={() => setLightboxIndex(null)}
        />
      )}
    </div>
  )
}
