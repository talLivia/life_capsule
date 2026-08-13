'use client'

/**
 * The per-category photo zone on the recording screen (MEDIA_GALLERY.md
 * §9.6): one persistent area below the recording UI, the same for every
 * question in the category, because the photos belong to the CATEGORY as a
 * whole (§2.2) — never to a take or a question.
 *
 * Keyed by category by the caller, so switching questions inside a category
 * neither moves nor reloads it.
 */

import { useCallback, useEffect, useRef, useState } from 'react'
import { ImagePlus, Loader2, X } from 'lucide-react'
import { toast } from 'react-hot-toast'
import { api, uploadPhoto } from '@/lib/api'
import type { MediaAsset } from '@/lib/types'

export function CategoryPhotoZone({ categoryId }: { categoryId: string }) {
  const inputRef = useRef<HTMLInputElement>(null)
  const [photos, setPhotos] = useState<MediaAsset[] | null>(null)
  const [uploading, setUploading] = useState(false)
  const [confirmingId, setConfirmingId] = useState<string | null>(null)

  const load = useCallback(async () => {
    try {
      setPhotos(await api.listMedia({ category: categoryId }))
    } catch {
      // A failed load renders the zone empty-but-usable rather than broken;
      // the next upload or open retries it.
      setPhotos([])
    }
  }, [categoryId])

  useEffect(() => {
    void load()
  }, [load])

  const handleFiles = async (files: FileList | null) => {
    if (!files || files.length === 0) return
    setUploading(true)
    try {
      // Sequential, not parallel: each presign+PUT is quick, and one failed
      // file should say WHICH file rather than failing a Promise.all.
      for (const file of Array.from(files)) {
        try {
          await uploadPhoto({ category: categoryId }, file)
        } catch (e: unknown) {
          const detail = (e as { response?: { data?: { detail?: string } } })?.response?.data?.detail
          toast.error(detail ?? `Couldn't upload ${file.name}`)
        }
      }
      await load()
    } finally {
      setUploading(false)
      if (inputRef.current) inputRef.current.value = ''
    }
  }

  const remove = async (photo: MediaAsset) => {
    if (confirmingId !== photo.id) {
      setConfirmingId(photo.id)
      return
    }
    setConfirmingId(null)
    try {
      await api.deleteMediaAsset(photo.id)
      setPhotos((current) => current?.filter((p) => p.id !== photo.id) ?? null)
    } catch {
      toast.error("Couldn't remove that photo — please try again")
    }
  }

  return (
    <section className="border-t border-edge pt-4">
      <div className="flex items-center justify-between gap-3">
        <div>
          <h2 className="text-sm text-ink font-medium">Photos for this chapter</h2>
          <p className="text-[11px] text-muted2 mt-0.5">
            They belong to the whole chapter, not one answer — family will see them
            beside it on the timeline.
          </p>
        </div>
        <button
          type="button"
          onClick={() => !uploading && inputRef.current?.click()}
          disabled={uploading}
          className="btn-secondary shrink-0"
        >
          {uploading ? <Loader2 size={16} className="animate-spin" /> : <ImagePlus size={16} />}
          Add photos
        </button>
      </div>

      {photos && photos.length > 0 && (
        <ul className="flex flex-wrap gap-2 mt-3">
          {photos.map((photo) => (
            <li key={photo.id} className="relative group">
              {/* eslint-disable-next-line @next/next/no-img-element -- presigned
                  thumbnail URLs; next/image caching buys nothing here. */}
              <img
                src={photo.url}
                alt={photo.caption ?? ''}
                className="w-20 h-20 object-cover rounded-lg border border-edge"
              />
              <button
                type="button"
                onClick={() => remove(photo)}
                aria-label="Remove photo"
                className={`absolute -top-1.5 -right-1.5 rounded-full p-1 text-ink transition-opacity ${
                  confirmingId === photo.id
                    ? 'bg-red-500 opacity-100'
                    : 'bg-black/60 opacity-0 group-hover:opacity-100'
                }`}
                title={confirmingId === photo.id ? 'Click again to remove' : 'Remove'}
              >
                <X size={11} />
              </button>
            </li>
          ))}
        </ul>
      )}

      <input
        ref={inputRef}
        type="file"
        accept="image/jpeg,image/png,image/webp"
        multiple
        className="hidden"
        onChange={(e) => void handleFiles(e.target.files)}
      />
    </section>
  )
}
