'use client'

/**
 * The one entity-portrait control (docs/MEDIA_GALLERY.md §9.6): a circle
 * showing the primary photo when one exists and the initials placeholder when
 * none does, clickable to upload a new one — which becomes the face
 * (make_primary) so the click visibly did something.
 *
 * Used by the extraction panel and the family tree's person card. ONE
 * component on purpose: the §9.6 rule is that the tree reuses the extraction
 * panel's flow, never a second mechanism.
 */

import { useRef, useState } from 'react'
import { Camera, Loader2 } from 'lucide-react'
import { toast } from 'react-hot-toast'
import { uploadPhoto } from '@/lib/api'

export function EntityPortrait({
  entityId,
  name,
  photoUrl,
  size = 40,
  onChanged,
  readOnly = false,
}: {
  entityId: string
  name: string
  photoUrl?: string | null
  /** Diameter in px. Callers match whatever circle already exists on their
   *  surface — this control never redesigns the node around it. */
  size?: number
  /** Fired after a successful upload so the caller can refetch the payload
   *  the new photo_url rides on. */
  onChanged?: () => void
  /** A family viewer sees the portrait, never the upload (the backend
   *  refuses the write anyway — this keeps the UI honest about it). */
  readOnly?: boolean
}) {
  const inputRef = useRef<HTMLInputElement>(null)
  const [uploading, setUploading] = useState(false)
  // The new face, shown immediately — the caller's refetch replaces it with
  // the server's own resolved URL on the next payload.
  const [localUrl, setLocalUrl] = useState<string | null>(null)

  const shown = localUrl ?? photoUrl

  if (readOnly) {
    return (
      <span
        role="img"
        aria-label={`Photo of ${name}`}
        className="relative shrink-0 rounded-full overflow-hidden inline-flex"
        style={{ width: size, height: size }}
      >
        {shown ? (
          // eslint-disable-next-line @next/next/no-img-element
          <img src={shown} alt={name} className="w-full h-full object-cover" />
        ) : (
          <span className="w-full h-full flex items-center justify-center bg-white/8 text-white/70 text-xs font-semibold">
            {name.slice(0, 2)}
          </span>
        )}
      </span>
    )
  }

  const pick = () => {
    if (!uploading) inputRef.current?.click()
  }

  const handleFile = async (file: File | undefined) => {
    if (!file) return
    setUploading(true)
    try {
      const asset = await uploadPhoto({ entity_id: entityId }, file, { makePrimary: true })
      setLocalUrl(asset.url)
      onChanged?.()
      toast.success('Photo saved')
    } catch (e: unknown) {
      const detail = (e as { response?: { data?: { detail?: string } } })?.response?.data?.detail
      toast.error(detail ?? "Couldn't upload that photo — please try again")
    } finally {
      setUploading(false)
      // Same file picked twice must fire onChange twice.
      if (inputRef.current) inputRef.current.value = ''
    }
  }

  return (
    <button
      type="button"
      onClick={pick}
      disabled={uploading}
      title={shown ? 'Change photo' : 'Add a photo'}
      aria-label={shown ? `Change photo of ${name}` : `Add a photo of ${name}`}
      className="relative shrink-0 rounded-full overflow-hidden group focus:outline-none focus:ring-2 focus:ring-primary-400"
      style={{ width: size, height: size }}
    >
      {shown ? (
        /* Presigned URLs are short-lived and per-request; next/image caching
           works against them and buys nothing for a thumbnail this size. */
        // eslint-disable-next-line @next/next/no-img-element
        <img src={shown} alt={name} className="w-full h-full object-cover" />
      ) : (
        /* No photo yet: the circle IS the empty-state upload control — a
           centered camera filling it, replacing the initials outright (the
           name sits right beside this control on every surface that mounts
           it, so the initials carried no information the row lacks). A
           corner-badge draft of this read as broken, not intentional. */
        <span className="w-full h-full flex items-center justify-center bg-white/8">
          <Camera
            size={Math.max(14, Math.round(size * 0.45))}
            className="text-white/75 group-hover:text-white transition-colors"
          />
        </span>
      )}
      {/* Hover/focus on an existing photo offers the change; mid-upload the
          overlay holds regardless so the spinner cannot vanish when the
          pointer drifts. */}
      <span
        className={`absolute inset-0 flex items-center justify-center bg-surface-950/60 transition-opacity ${
          uploading
            ? 'opacity-100'
            : shown
              ? 'opacity-0 group-hover:opacity-100 group-focus:opacity-100'
              : 'opacity-0'
        }`}
      >
        {uploading ? (
          <Loader2 size={14} className="animate-spin text-white" />
        ) : (
          <Camera size={14} className="text-white" />
        )}
      </span>
      <input
        ref={inputRef}
        type="file"
        accept="image/jpeg,image/png,image/webp"
        className="hidden"
        onChange={(e) => handleFile(e.target.files?.[0])}
      />
    </button>
  )
}
