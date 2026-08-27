'use client'

// Bulk import — full-page view (BULK_IMPORT_PLAN UI round, 2026-08-28).
// Replaces the inline Settings panel: long-running batches are "ongoing
// work you can leave and come back to", so they get a real page. The table
// is a pure renderer over GET /batches/{id} — every row status is derived
// server-side (derive_rows), and the existing poll makes it live.

import { useCallback, useEffect, useRef, useState } from 'react'
import { apiClient } from '../lib/api'

type Row = {
  index: number
  line: number
  question_id: string
  question_text: string
  filename: string
  status: string
  error?: string | null
}
type Batch = {
  id: string
  state: string
  files: Record<string, { size?: number; staged?: boolean }>
  rows: Row[]
  unmapped_files: string[]
  file_states: Record<string, { state: string; error?: string | null }>
}

const BASE = '/api/v1/bulk-import'

const STATUS_LABEL: Record<string, string> = {
  ready_to_import: 'matched — will import',
  no_file_yet: 'no file provided yet — will be skipped',
  excluded: 'excluded from this batch',
  unknown_question: 'unknown question id',
  not_a_video: 'not a video file',
  duplicate: 'duplicate filename',
  pending: 'waiting…',
  ingesting: 'importing…',
  done: 'done',
  failed: 'failed',
}
const STATUS_CLASS: Record<string, string> = {
  ready_to_import: 'text-green-600',
  done: 'text-green-600',
  ingesting: 'text-primary-500 animate-pulse',
  pending: 'text-muted',
  no_file_yet: 'text-amber-600',
  excluded: 'text-muted2 line-through',
  failed: 'text-red-600',
  unknown_question: 'text-red-600',
  not_a_video: 'text-red-600',
  duplicate: 'text-red-600',
}

export default function BulkImportPage({ isGuest }: { isGuest: boolean }) {
  const [batch, setBatch] = useState<Batch | null>(null)
  const [busy, setBusy] = useState(false)
  const [staging, setStaging] = useState<Record<string, string>>({})
  const [banner, setBanner] = useState<{ kind: 'ok' | 'err' | 'info'; text: string } | null>(null)
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null)

  const refresh = useCallback(async (id?: string) => {
    const bid = id || batch?.id
    if (!bid) return
    const r = await apiClient.get(`${BASE}/batches/${bid}`)
    setBatch(r.data)
  }, [batch?.id])

  useEffect(() => {
    if (isGuest) return
    apiClient
      .get(`${BASE}/batches`)
      .then(r => {
        const open = (r.data as Batch[]).find(b =>
          ['staging', 'validated', 'running', 'done_with_failures'].includes(b.state)
        )
        if (open) setBatch(open)
      })
      .catch(() =>
        setBanner({ kind: 'err', text: 'Could not load batches — are you still logged in?' })
      )
  }, [isGuest])

  useEffect(() => {
    if (batch?.state !== 'running') return
    pollRef.current = setInterval(() => refresh().catch(() => {}), 4000)
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

  const downloadTemplate = async () => {
    const r = await apiClient.get(`${BASE}/template.csv`, { responseType: 'blob' })
    const url = URL.createObjectURL(r.data as Blob)
    const a = document.createElement('a')
    a.href = url
    a.download = 'bulk_import_template.csv'
    a.click()
    URL.revokeObjectURL(url)
  }

  const uploadMapping = async (files: FileList | null) => {
    if (!files?.length) return
    setBusy(true)
    setBanner({ kind: 'info', text: 'Reading the mapping…' })
    try {
      const b = await ensureBatch()
      const form = new FormData()
      form.append('file', files[0])
      const r = await apiClient.post(`${BASE}/batches/${b.id}/mapping`, form, {
        headers: { 'Content-Type': 'multipart/form-data' },
      })
      setBatch(r.data)
      setBanner({ kind: 'ok', text: `Mapping loaded — ${r.data.rows.length} row(s).` })
    } catch (err: unknown) {
      const status = (err as { response?: { status?: number } })?.response?.status
      setBanner({ kind: 'err', text: `Mapping upload failed${status ? ` (HTTP ${status})` : ''} — is it the filled CSV template?` })
    } finally {
      setBusy(false)
    }
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
          // Bounded retry on the app's own 60/min rate limit.
          for (let attempt = 0; ; attempt++) {
            try {
              await apiClient.put(`${BASE}/batches/${b.id}/files/${encodeURIComponent(f.name)}`, form, {
                headers: { 'Content-Type': 'multipart/form-data' },
              })
              break
            } catch (err: unknown) {
              const resp = (err as { response?: { status?: number; headers?: Record<string, string> } })?.response
              if (resp?.status === 429 && attempt < 3) {
                const wait = Number(resp.headers?.['retry-after']) || 15
                setStaging(prev => ({ ...prev, [f.name]: `rate-limited — retrying in ${wait}s…` }))
                await new Promise(res => setTimeout(res, wait * 1000))
                continue
              }
              throw err
            }
          }
          setStaging(prev => ({ ...prev, [f.name]: 'done' }))
        } catch (err: unknown) {
          failed += 1
          const st = (err as { response?: { status?: number } })?.response?.status
          setStaging(prev => ({ ...prev, [f.name]: `failed${st ? ` (${st})` : ''}` }))
        }
      }
      await refresh(b.id)
      setBanner(
        failed
          ? { kind: 'err', text: `${files.length - failed} uploaded, ${failed} failed — re-select the failed files to retry.` }
          : { kind: 'ok', text: `${files.length} file(s) uploaded.` }
      )
    } catch (err: unknown) {
      const status = (err as { response?: { status?: number } })?.response?.status
      setBanner({ kind: 'err', text: `Upload failed${status ? ` (HTTP ${status})` : ''}.` })
    } finally {
      setBusy(false)
    }
  }

  const setExcluded = async (row: Row, excluded: boolean) => {
    if (!batch) return
    const r = await apiClient.patch(`${BASE}/batches/${batch.id}/rows/${row.index}`, { excluded })
    setBatch(r.data)
  }

  const start = async () => {
    if (!batch) return
    try {
      const r = await apiClient.post(`${BASE}/batches/${batch.id}/start`)
      setBatch(r.data)
      setBanner({ kind: 'ok', text: 'Import started — you can leave this page; it keeps running.' })
    } catch (err: unknown) {
      const detail = (err as { response?: { data?: { detail?: string } } })?.response?.data?.detail
      setBanner({ kind: 'err', text: detail || 'Could not start the import.' })
    }
  }

  const retryFile = async (name: string) => {
    if (!batch) return
    const r = await apiClient.post(`${BASE}/batches/${batch.id}/files/${encodeURIComponent(name)}/retry`)
    setBatch(r.data)
  }

  const rows = batch?.rows || []
  const importable = rows.filter(r => r.status === 'ready_to_import').length
  const running = batch?.state === 'running'

  return (
    <div className="max-w-4xl mx-auto flex flex-col gap-6 p-4">
      <div>
        <h1 className="text-2xl font-bold text-ink">Import old recordings</h1>
        <p className="text-sm text-muted mt-1">
          Bring videos from another system through the normal pipeline. Imports skip the
          review step; extracted details stay editable afterwards. This page keeps working
          server-side even if you close the tab — come back any time.
        </p>
      </div>

      <div className="card flex flex-col gap-3">
        <h2 className="font-bold text-ink">Step 1 — download the question list</h2>
        <p className="text-xs text-muted">
          One row per interview question. Put a filename next to each question a video
          answers. Several takes for one question: separate filenames with ; in one cell,
          or duplicate the row. Leave the rest empty.
        </p>
        <button className="btn-secondary w-fit" disabled={isGuest} onClick={downloadTemplate}>
          Download template (CSV)
        </button>
      </div>

      <div className="card flex flex-col gap-3">
        <h2 className="font-bold text-ink">Step 2 — upload the filled mapping</h2>
        <label className="btn-secondary w-fit cursor-pointer">
          {busy ? 'Working…' : 'Upload mapping CSV'}
          <input type="file" accept=".csv,text/csv" className="hidden"
            disabled={isGuest || busy || running}
            onChange={e => uploadMapping(e.target.files)} />
        </label>
      </div>

      <div className="card flex flex-col gap-3">
        <h2 className="font-bold text-ink">Step 3 — upload the video files</h2>
        <p className="text-xs text-muted">
          Files match to rows by filename as they upload — watch the table below.
        </p>
        <label className="btn-secondary w-fit cursor-pointer">
          {busy ? 'Uploading…' : 'Select video files'}
          <input type="file" multiple accept="video/*" className="hidden"
            disabled={isGuest || busy || running}
            onChange={e => stageFiles(e.target.files)} />
        </label>
        {Object.keys(staging).length > 0 && (
          <div className="text-xs text-muted flex flex-col gap-0.5 max-h-32 overflow-y-auto">
            {Object.entries(staging).map(([name, st]) => (
              <span key={name} className={st.startsWith('failed') ? 'text-red-600' : ''}>
                {name}: {st}
              </span>
            ))}
          </div>
        )}
      </div>

      {banner && (
        <div className={
          banner.kind === 'ok' ? 'text-sm font-semibold text-green-600'
            : banner.kind === 'err' ? 'text-sm font-semibold text-red-600'
              : 'text-sm font-semibold text-muted animate-pulse'
        }>
          {banner.text}
        </div>
      )}

      {rows.length > 0 && (
        <div className="card flex flex-col gap-3">
          <div className="flex items-center justify-between">
            <h2 className="font-bold text-ink">
              Mapping — {importable} of {rows.length} row(s) will import
            </h2>
            <button className="btn-secondary text-xs" onClick={() => refresh()}>
              Refresh now
            </button>
          </div>
          <div className="overflow-x-auto">
            <table className="w-full text-xs">
              <thead>
                <tr className="text-left text-muted border-b">
                  <th className="py-1 pr-2">Question</th>
                  <th className="py-1 pr-2">File</th>
                  <th className="py-1 pr-2">Status</th>
                  <th className="py-1" />
                </tr>
              </thead>
              <tbody>
                {rows.map(r => (
                  <tr key={r.index} className="border-b border-dashed align-top">
                    <td className="py-1.5 pr-2">
                      <span className="block text-ink">{r.question_text || r.question_id}</span>
                      <span className="text-muted2">{r.question_id}</span>
                    </td>
                    <td className="py-1.5 pr-2 break-all">{r.filename}</td>
                    <td className={`py-1.5 pr-2 ${STATUS_CLASS[r.status] || ''}`}>
                      {STATUS_LABEL[r.status] || r.status}
                      {r.error ? ` — ${r.error}` : ''}
                      {r.status === 'failed' && !running && (
                        <button className="underline ml-2" onClick={() => retryFile(r.filename)}>
                          retry
                        </button>
                      )}
                    </td>
                    <td className="py-1.5 text-right">
                      {!running && (r.status === 'excluded' ? (
                        <button className="underline text-muted" onClick={() => setExcluded(r, false)}>
                          restore
                        </button>
                      ) : ['ready_to_import', 'no_file_yet', 'unknown_question', 'not_a_video', 'duplicate'].includes(r.status) && (
                        <button className="underline text-muted" onClick={() => setExcluded(r, true)}>
                          remove
                        </button>
                      ))}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          {(batch?.unmapped_files?.length ?? 0) > 0 && (
            <p className="text-xs text-amber-600">
              Uploaded but not in the mapping (ignored): {batch?.unmapped_files.join(', ')}
            </p>
          )}
        </div>
      )}

      <div className="card flex flex-col gap-2">
        <h2 className="font-bold text-ink">Step 4 — start</h2>
        <button
          className="btn-primary w-fit"
          disabled={isGuest || batch?.state !== 'validated' || importable === 0}
          onClick={start}
        >
          {running ? 'Importing…' : `Start import (${importable} file(s))`}
        </button>
        {batch && ['done', 'done_with_failures'].includes(batch.state) && (
          <p className="text-sm font-semibold text-ink">
            Finished — see per-row results above.
          </p>
        )}
      </div>
    </div>
  )
}
