'use client'

/**
 * Full-screen viewer for one gallery (docs/MEDIA_GALLERY.md §4) — the photos
 * are pinned at open time, so a hover elsewhere cannot swap the deck under
 * the viewer. Built once here for the timeline; /talk's Phase 8 gallery
 * reuses it rather than growing a second one.
 */

import { useCallback, useEffect, useState } from 'react'
import { createPortal } from 'react-dom'
import { ChevronLeft, ChevronRight, X } from 'lucide-react'
import type { MediaAsset } from '@/lib/types'

export function PhotoLightbox({
  photos,
  initialIndex,
  onClose,
}: {
  photos: MediaAsset[]
  initialIndex: number
  onClose: () => void
}) {
  const [index, setIndex] = useState(initialIndex)
  const [mounted, setMounted] = useState(false)

  useEffect(() => setMounted(true), [])

  const step = useCallback(
    (delta: number) => {
      setIndex((current) => {
        const next = current + delta
        return next < 0 || next >= photos.length ? current : next
      })
    },
    [photos.length],
  )

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose()
      if (e.key === 'ArrowRight') step(1)
      if (e.key === 'ArrowLeft') step(-1)
    }
    window.addEventListener('keydown', onKey)
    const previous = document.body.style.overflow
    document.body.style.overflow = 'hidden'
    return () => {
      window.removeEventListener('keydown', onKey)
      document.body.style.overflow = previous
    }
  }, [onClose, step])

  const photo = photos[index]
  if (!mounted || !photo) return null

  return createPortal(
    <div
      className="fixed inset-0 z-[70] flex flex-col items-center justify-center bg-surface-950/95 backdrop-blur-md p-4"
      role="dialog"
      aria-modal="true"
      aria-label="Photo viewer"
      onClick={onClose}
    >
      <button
        onClick={onClose}
        aria-label="Close"
        className="absolute top-4 right-4 text-gray-400 hover:text-white"
      >
        <X size={20} />
      </button>

      {/* Stopping propagation keeps clicks on the photo and its chrome from
          reaching the backdrop's close handler. */}
      <figure
        className="flex flex-col items-center gap-3"
        onClick={(e) => e.stopPropagation()}
      >
        {/* A FIXED stage, whatever each photo's own dimensions: the photo
            letterboxes/pillarboxes inside it (object-contain), so stepping
            through a gallery of mixed portrait/landscape shots never
            resizes the layout — which is what kept moving the nav buttons
            out from under the pointer. */}
        <div className="w-[min(86vw,56rem)] h-[68vh] flex items-center justify-center">
          {/* eslint-disable-next-line @next/next/no-img-element */}
          <img
            src={photo.url}
            alt={photo.caption ?? ''}
            className="max-w-full max-h-full object-contain rounded-lg"
          />
        </div>
        {/* Always rendered at a constant height — a caption appearing on one
            photo and not the next must not shift the buttons either. */}
        <figcaption className="text-center h-10">
          {photo.caption && (
            <p dir="auto" className="text-sm text-gray-200">{photo.caption}</p>
          )}
          {photo.taken_year && (
            <p className="text-xs text-gray-500 mt-0.5">{photo.taken_year}</p>
          )}
        </figcaption>
      </figure>

      {photos.length > 1 && (
        <div
          className="flex items-center gap-4 mt-4"
          onClick={(e) => e.stopPropagation()}
        >
          <button
            onClick={() => step(-1)}
            disabled={index === 0}
            aria-label="Previous photo"
            className="p-2 rounded-full bg-white/8 text-white disabled:opacity-30 hover:bg-white/15"
          >
            <ChevronLeft size={18} />
          </button>
          <span className="text-xs text-gray-500">
            {index + 1} / {photos.length}
          </span>
          <button
            onClick={() => step(1)}
            disabled={index === photos.length - 1}
            aria-label="Next photo"
            className="p-2 rounded-full bg-white/8 text-white disabled:opacity-30 hover:bg-white/15"
          >
            <ChevronRight size={18} />
          </button>
        </div>
      )}
    </div>,
    document.body
  )
}
