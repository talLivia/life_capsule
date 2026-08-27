'use client'

// Bulk import of legacy videos (docs/BULK_IMPORT_PLAN.md §4).
// The batch lives server-side (BulkImportBatch), so this panel is a thin
// view over it: closing the tab loses nothing — reopening Settings finds
// the draft/running batch via GET /batches and resumes.

import { useCallback, useEffect, useRef, useState } from 'react'
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
  // Per-file staging progress + a persistent status banner: live testing
  // found every failure path here was SILENT (no catch on the mapping
  // upload, disabled inputs with no explanation, resume errors swallowed).
  const [staging, setStaging] = useState<Record<string, string>>({})
  const [banner, setBanner] = useState<{ kind: 'ok' | 'err' | 'info'; text: string } | null>(null)
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
      .catch(() => {
        setBanner({ kind: 'err', text: 'Could not load your import batches — are you still logged in? Refresh and try again.' })
      })
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
    setBanner({ kind: 'info', text: `Uploading ${files.length} file(s)…` })
    let failed = 0
    try {
      const b = await ensureBatch()
      for (const f of Array.from(files)) {
        setStaging(prev => ({ ...prev, [f.name]: 'uploading…' }))
        try {
          const form = new FormData()
          form.append('file', f)
          await apiClient.put(`${BASE}/batches/${b.id}/files/${encodeURIComponent(f.name)}`, form, {
            // The instance default is application/json, under which axios
            // JSON-converts FormData (and throws on File payloads). Multipart
            // here lets the browser set the boundary.
            headers: { 'Content-Type': 'multipart/form-data' },
          })
          setStaging(prev => ({ ...prev, [f.name]: 'done' }))
        } catch (err: unknown) {
          failed += 1
          const detail = (err as { response?: { status?: number } })?.response?.status
          setStaging(prev => ({ ...prev, [f.name]: `failed${detail ? ` (${detail})` : ''}` }))
        }
      }
      await refresh(b.id)
      setBanner(
        failed
          ? { kind: 'err', text: `${files.length - failed} uploaded, ${failed} failed — re-select the failed files to retry them.` }
          : { kind: 'ok', text: `${files.length} file(s) staged.` }
      )
    } catch (err: unknown) {
      const status = (err as { response?: { status?: number } })?.response?.status
      setBanner({ kind: 'err', text: `Upload failed${status ? ` (HTTP ${status})` : ''} — check you are logged in and try again.` })
    } finally {
      setBusy(false)
    }
  }

  const uploadMapping = async (files: FileList | null) => {
    if (!files?.length) return
    if (!batch) {
      setBanner({ kind: 'err', text: 'Stage your video files first (step 2) — the mapping needs a batch to attach to.' })
      return
    }
    setBusy(true)
    setBanner({ kind: 'info', text: 'Checking the mapping…' })
    try {
      const form = new FormData()
      form.append('file', files[0])
      const r = await apiClient.post(`${BASE}/batches/${batch.id}/mapping`, form, {
        headers: { 'Content-Type': 'multipart/form-data' },
      })
      setBatch(r.data)
      const ok = r.data.state === 'validated'
      setBanner(
        ok
          ? { kind: 'ok', text: `Mapping valid — ${r.data.mapping?.length ?? 0} file(s) ready to import.` }
          : { kind: 'err', text: 'Mapping has problems — fix the rows listed below and upload it again.' }
      )
    } catch (err: unknown) {
      // This path used to be SILENT (no catch): any transport/server error
      // vanished. Now it always lands in the banner.
      const status = (err as { response?: { status?: number } })?.response?.status
      setBanner({ kind: 'err', text: `Mapping upload failed${status ? ` (HTTP ${status})` : ''} — is it the filled CSV template?` })
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

      {banner && (
        <div
          className={
            banner.kind === 'ok'
              ? 'text-xs font-semibold text-green-600'
              : banner.kind === 'err'
                ? 'text-xs font-semibold text-red-600'
                : 'text-xs font-semibold text-muted animate-pulse'
          }
        >
          {banner.text}
        </div>
      )}

      <label className="btn-secondary w-fit cursor-pointer">
        2. {busy ? 'Uploading…' : 'Select video files'}
        <input
          type="file"
          multiple
          accept="video/*"
          className="hidden"
          disabled={isGuest || busy || batch?.state === 'running'}
          onChange={e => stageFiles(e.target.files)}
        />
      </label>
      {Object.keys(staging).length > 0 && (
        <div className="text-xs text-muted flex flex-col gap-0.5 max-h-40 overflow-y-auto">
          {Object.entries(staging).map(([name, st]) => (
            <span key={name} className={st.startsWith('failed') ? 'text-red-600' : ''}>
              {name}: {st}
            </span>
          ))}
        </div>
      )}
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
