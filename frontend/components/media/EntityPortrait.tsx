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

/** Up to two initials — same rule as the tree's SVG nodes. */
function initials(name: string): string {
  const parts = name.trim().split(/\s+/).filter(Boolean)
  if (parts.length === 0) return '?'
  if (parts.length === 1) return parts[0].slice(0, 2)
  return parts[0][0] + parts[1][0]
}

export function EntityPortrait({
  entityId,
  name,
  photoUrl,
  size = 40,
  onChanged,
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
}) {
  const inputRef = useRef<HTMLInputElement>(null)
  const [uploading, setUploading] = useState(false)
  // The new face, shown immediately — the caller's refetch replaces it with
  // the server's own resolved URL on the next payload.
  const [localUrl, setLocalUrl] = useState<string | null>(null)

  const shown = localUrl ?? photoUrl

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
        <span className="w-full h-full flex items-center justify-center bg-white/8 text-white text-xs font-semibold">
          {initials(name)}
        </span>
      )}
      {/* The affordance: visible on hover/focus, never covering the face
          otherwise. Always visible while there is no photo yet — an empty
          circle explains nothing on its own. */}
      <span
        className={`absolute inset-0 flex items-center justify-center bg-surface-950/60 transition-opacity ${
          shown ? 'opacity-0 group-hover:opacity-100 group-focus:opacity-100' : 'opacity-0 group-hover:opacity-100'
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
