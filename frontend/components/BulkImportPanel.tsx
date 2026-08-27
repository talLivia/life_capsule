'use client'

// Bulk import of legacy videos (docs/BULK_IMPORT_PLAN.md §4).
// The batch lives server-side (BulkImportBatch), so this panel is a thin
// view over it: closing the tab loses nothing — reopening Settings finds
// the draft/running batch via GET /batches and resumes.

import { useCallback, useEffect, useRef, useState } from 'react'
import toast from 'react-hot-toast'
import { apiClient } from '../lib/api'

type FileState = { state: string; error?: string | null }
type Batch = {
  id: string
  state: string
  files: Record<string, { size?: number; staged?: boolean }>
  report?: { errors: Record<string, unknown>[]; warnings: Record<string, unknown>[] } | null
  file_states: Record<string, FileState>
}

const BASE = '/api/v1/bulk-import'

export default function BulkImportPanel({ isGuest }: { isGuest: boolean }) {
  const [batch, setBatch] = useState<Batch | null>(null)
  const [busy, setBusy] = useState(false)
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null)

  const refresh = useCallback(async (id: string) => {
    const r = await apiClient.get(`${BASE}/batches/${id}`)
    setBatch(r.data)
    return r.data as Batch
  }, [])

  // Resume: find an unfinished batch on mount.
  useEffect(() => {
    if (isGuest) return
    apiClient
      .get(`${BASE}/batches`)
      .then(r => {
        const open = (r.data as Batch[]).find(b =>
          ['staging', 'validated', 'running'].includes(b.state)
        )
        if (open) setBatch(open)
      })
      .catch(() => {})
  }, [isGuest])

  // Poll while running; server state is the truth.
  useEffect(() => {
    if (batch?.state !== 'running') return
    pollRef.current = setInterval(() => refresh(batch.id).catch(() => {}), 4000)
    return () => {
      if (pollRef.current) clearInterval(pollRef.current)
    }
  }, [batch?.id, batch?.state, refresh])

  const ensureBatch = async (): Promise<Batch> => {
    if (batch && ['staging', 'validated'].includes(batch.state)) return batch
    const r = await apiClient.post(`${BASE}/batches`)
    setBatch(r.data)
    return r.data
  }

  const stageFiles = async (files: FileList | null) => {
    if (!files?.length) return
    setBusy(true)
    try {
      const b = await ensureBatch()
      for (const f of Array.from(files)) {
        const form = new FormData()
        form.append('file', f)
        await apiClient.put(`${BASE}/batches/${b.id}/files/${encodeURIComponent(f.name)}`, form, {
          // The instance default is application/json, under which axios
          // JSON-converts FormData (and throws on File payloads — the
          // "Upload failed" bug). Multipart here lets the browser set the
          // boundary.
          headers: { 'Content-Type': 'multipart/form-data' },
        })
      }
      await refresh(b.id)
      toast.success(`${files.length} file(s) staged`)
    } catch {
      toast.error('Upload failed — re-select the files to resume')
    } finally {
      setBusy(false)
    }
  }

  const uploadMapping = async (files: FileList | null) => {
    if (!files?.length || !batch) return
    setBusy(true)
    try {
      const form = new FormData()
      form.append('file', files[0])
      const r = await apiClient.post(`${BASE}/batches/${batch.id}/mapping`, form, {
        headers: { 'Content-Type': 'multipart/form-data' },
      })
      setBatch(r.data)
      toast[r.data.state === 'validated' ? 'success' : 'error'](
        r.data.state === 'validated'
          ? 'Mapping valid — ready to start'
          : 'Mapping has problems — see the report below'
      )
    } finally {
      setBusy(false)
    }
  }

  const start = async () => {
    if (!batch) return
    const r = await apiClient.post(`${BASE}/batches/${batch.id}/start`)
    setBatch(r.data)
  }

  const retry = async (name: string) => {
    if (!batch) return
    const r = await apiClient.post(
      `${BASE}/batches/${batch.id}/files/${encodeURIComponent(name)}/retry`
    )
    setBatch(r.data)
  }

  const states = batch?.file_states || {}
  const counts = Object.values(states).reduce<Record<string, number>>((acc, s) => {
    acc[s.state] = (acc[s.state] || 0) + 1
    return acc
  }, {})

  return (
    <div className="card flex flex-col gap-4 mt-6">
      <h2 className="text-xl font-bold text-ink">Import old recordings</h2>
      <div className="divider" />
      <p className="text-xs text-muted">
        Bring videos from another system through the normal pipeline: download the
        question list, fill in which file answers which question (several files per
        question = several takes), upload everything, and start. Imports skip the
        review step; you can edit extracted details afterwards.
      </p>

      <button
        className="btn-secondary w-fit"
        disabled={isGuest}
        onClick={async () => {
          // A bare <a href> carries no Authorization header (it 401'd in
          // live testing) — fetch through the authenticated client instead.
          const r = await apiClient.get(`${BASE}/template.csv`, { responseType: 'blob' })
          const url = URL.createObjectURL(r.data as Blob)
          const a = document.createElement('a')
          a.href = url
          a.download = 'bulk_import_template.csv'
          a.click()
          URL.revokeObjectURL(url)
        }}
      >
        1. Download question template (CSV)
      </button>

      <label className="btn-secondary w-fit cursor-pointer">
        2. Select video files
        <input
          type="file"
          multiple
          accept="video/*"
          className="hidden"
          disabled={isGuest || busy || batch?.state === 'running'}
          onChange={e => stageFiles(e.target.files)}
        />
      </label>
      {batch && (
        <span className="text-xs text-muted">
          {Object.keys(batch.files || {}).length} file(s) staged · batch {batch.state}
        </span>
      )}

      <label className="btn-secondary w-fit cursor-pointer">
        3. Upload filled mapping CSV
        <input
          type="file"
          accept=".csv,text/csv"
          className="hidden"
          disabled={isGuest || busy || !batch || batch.state === 'running'}
          onChange={e => uploadMapping(e.target.files)}
        />
      </label>

      {batch?.report && batch.report.errors.length > 0 && (
        <div className="text-xs text-red-600 flex flex-col gap-1">
          {batch.report.errors.map((e, i) => (
            <span key={i}>{JSON.stringify(e)}</span>
          ))}
        </div>
      )}
      {batch?.report && batch.report.warnings.length > 0 && (
        <div className="text-xs text-amber-600 flex flex-col gap-1">
          {batch.report.warnings.map((w, i) => (
            <span key={i}>{JSON.stringify(w)}</span>
          ))}
        </div>
      )}

      <button
        className="btn-primary w-fit"
        disabled={isGuest || batch?.state !== 'validated'}
        onClick={start}
      >
        4. Start import
      </button>

      {batch && ['running', 'done', 'done_with_failures'].includes(batch.state) && (
        <div className="text-xs flex flex-col gap-1">
          <span className="font-semibold">
            {batch.state === 'running' ? 'Importing…' : 'Finished'}
            {' — '}
            {Object.entries(counts)
              .map(([k, v]) => `${v} ${k}`)
              .join(', ')}
          </span>
          {Object.entries(states)
            .filter(([, s]) => s.state === 'failed')
            .map(([name, s]) => (
              <span key={name} className="text-red-600">
                {name}: {s.error || 'failed'}{' '}
                <button className="underline" onClick={() => retry(name)}>
                  retry
                </button>
              </span>
            ))}
        </div>
      )}
    </div>
  )
}
