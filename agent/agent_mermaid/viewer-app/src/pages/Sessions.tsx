import { useEffect, useState, useMemo } from 'react'
import { Link } from 'react-router-dom'

const API_BASE = ''
const PAGE_SIZES = [10, 25, 50, 100]

interface SessionRow {
  session_id: string
  first_ts?: number
  last_ts?: number
  last_tool?: string
  event_count?: number
  duration_sec?: number
  last_message?: string
  agent_name?: string
}

function formatTs(ts: number): string {
  try {
    return new Date(ts * 1000).toLocaleString(undefined, {
      dateStyle: 'short',
      timeStyle: 'medium',
    })
  } catch {
    return '—'
  }
}

function formatDuration(sec: number): string {
  if (sec <= 0) return '00:00'
  const m = Math.floor(sec / 60)
  const s = Math.floor(sec % 60)
  const h = Math.floor(m / 60)
  const mm = m % 60
  if (h > 0) return `${h.toString().padStart(2, '0')}:${mm.toString().padStart(2, '0')}:${s.toString().padStart(2, '0')}`
  return `${mm.toString().padStart(2, '0')}:${s.toString().padStart(2, '0')}`
}

export function Sessions() {
  const [sessions, setSessions] = useState<SessionRow[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [page, setPage] = useState(1)
  const [pageSize, setPageSize] = useState(25)
  const [selectedIds, setSelectedIds] = useState<Set<string>>(new Set())
  const [deleting, setDeleting] = useState(false)

  const loadSessions = () => {
    setLoading(true)
    setError(null)
    fetch(`${API_BASE}/api/connections`)
      .then((r) => (r.ok ? r.json() : Promise.reject(new Error('Failed to load sessions'))))
      .then((data) => setSessions(data.sessions ?? []))
      .catch((e) => setError(e.message))
      .finally(() => setLoading(false))
  }

  useEffect(() => {
    loadSessions()
  }, [])

  const totalPages = Math.max(1, Math.ceil(sessions.length / pageSize))
  const paginatedSessions = useMemo(() => {
    const start = (page - 1) * pageSize
    return sessions.slice(start, start + pageSize)
  }, [sessions, page, pageSize])

  useEffect(() => {
    if (page > totalPages) setPage(1)
  }, [page, totalPages])

  const deleteSession = async (sessionId: string) => {
    if (!confirm(`Delete session ${sessionId.slice(0, 12)}…?`)) return
    const r = await fetch(`${API_BASE}/api/connections/${encodeURIComponent(sessionId)}`, { method: 'DELETE' })
    if (!r.ok) {
      const err = await r.json().catch(() => ({}))
      setError(err?.error ?? 'Failed to delete session')
      return
    }
    setSessions((prev) => prev.filter((s) => s.session_id !== sessionId))
    setSelectedIds((prev) => {
      const next = new Set(prev)
      next.delete(sessionId)
      return next
    })
  }

  const toggleSelect = (sessionId: string) => {
    setSelectedIds((prev) => {
      const next = new Set(prev)
      if (next.has(sessionId)) next.delete(sessionId)
      else next.add(sessionId)
      return next
    })
  }

  const toggleSelectAllOnPage = () => {
    const allSelected = paginatedSessions.every((s) => selectedIds.has(s.session_id))
    setSelectedIds((prev) => {
      const next = new Set(prev)
      if (allSelected) paginatedSessions.forEach((s) => next.delete(s.session_id))
      else paginatedSessions.forEach((s) => next.add(s.session_id))
      return next
    })
  }

  const deleteSelected = async () => {
    if (selectedIds.size === 0) return
    if (!confirm(`Delete ${selectedIds.size} session(s)?`)) return
    setDeleting(true)
    setError(null)
    const ids = Array.from(selectedIds)
    for (const id of ids) {
      const r = await fetch(`${API_BASE}/api/connections/${encodeURIComponent(id)}`, { method: 'DELETE' })
      if (!r.ok) {
        const err = await r.json().catch(() => ({}))
        setError(err?.error ?? 'Failed to delete session')
        break
      }
    }
    loadSessions()
    setSelectedIds(new Set())
    setDeleting(false)
  }

  const now = Date.now() / 1000
  const isLive = (s: SessionRow) => (s.last_ts ?? 0) > now - 300

  return (
    <div className="page sessions-page">
      <h2>Sessions</h2>
      <p>Live and past MCP sessions.</p>
      {loading && <p>Loading…</p>}
      {error && <p style={{ color: '#f87171' }}>{error}</p>}
      {!loading && !error && (
        <>
          <div className="sessions-toolbar">
            {selectedIds.size > 0 && (
              <button
                type="button"
                onClick={deleteSelected}
                disabled={deleting}
                className="btn btn-danger"
              >
                {deleting ? 'Deleting…' : `Delete selected (${selectedIds.size})`}
              </button>
            )}
            <div className="sessions-pagination">
              <span className="sessions-range">
                {sessions.length === 0
                  ? '0 sessions'
                  : `${(page - 1) * pageSize + 1}–${Math.min(page * pageSize, sessions.length)} of ${sessions.length}`}
              </span>
              <label className="sessions-page-size">
                Per page:
                <select
                  value={pageSize}
                  onChange={(e) => {
                    setPageSize(Number(e.target.value))
                    setPage(1)
                  }}
                >
                  {PAGE_SIZES.map((n) => (
                    <option key={n} value={n}>
                      {n}
                    </option>
                  ))}
                </select>
              </label>
              <div className="sessions-nav">
                <button
                  type="button"
                  onClick={() => setPage((p) => Math.max(1, p - 1))}
                  disabled={page <= 1}
                  className="btn btn-secondary"
                >
                  Previous
                </button>
                <span className="sessions-page-num">
                  Page {page} of {totalPages}
                </span>
                <button
                  type="button"
                  onClick={() => setPage((p) => Math.min(totalPages, p + 1))}
                  disabled={page >= totalPages}
                  className="btn btn-secondary"
                >
                  Next
                </button>
              </div>
            </div>
          </div>
          <div className="table-wrap table-scroll">
            <table>
              <thead>
                <tr>
                  <th className="col-checkbox">
                    <input
                      type="checkbox"
                      checked={paginatedSessions.length > 0 && paginatedSessions.every((s) => selectedIds.has(s.session_id))}
                      onChange={toggleSelectAllOnPage}
                      title="Select all on page"
                    />
                  </th>
                  <th>Session ID</th>
                  <th>Agent</th>
                  <th>Status</th>
                  <th>Start</th>
                  <th>Duration</th>
                  <th>Last message</th>
                  <th></th>
                  <th></th>
                </tr>
              </thead>
              <tbody>
                {paginatedSessions.length === 0 ? (
                  <tr>
                    <td colSpan={9}>No sessions yet.</td>
                  </tr>
                ) : (
                  paginatedSessions.map((s) => (
                    <tr key={s.session_id}>
                      <td className="col-checkbox">
                        <input
                          type="checkbox"
                          checked={selectedIds.has(s.session_id)}
                          onChange={() => toggleSelect(s.session_id)}
                        />
                      </td>
                      <td>
                        <Link to={`/session/${s.session_id}`} className="font-mono">
                          {s.session_id.slice(0, 12)}
                          {s.session_id.length > 12 ? '…' : ''}
                        </Link>
                      </td>
                      <td>{s.agent_name ?? '—'}</td>
                      <td>
                        <span className={`badge ${isLive(s) ? 'badge-live' : 'badge-done'}`}>
                          {isLive(s) ? 'Live' : 'Completed'}
                        </span>
                      </td>
                      <td>{formatTs(s.first_ts ?? 0)}</td>
                      <td>{formatDuration(s.duration_sec ?? 0)}</td>
                      <td style={{ maxWidth: 200 }} className="truncate">
                        {(s.last_message ?? '').slice(0, 60)}
                        {(s.last_message?.length ?? 0) > 60 ? '…' : ''}
                      </td>
                      <td>
                        <Link to={`/session/${s.session_id}`}>View</Link>
                      </td>
                      <td>
                        <button
                          type="button"
                          onClick={() => deleteSession(s.session_id)}
                          className="text-red-400 hover:text-red-300 text-sm"
                          title="Delete session"
                        >
                          Delete
                        </button>
                      </td>
                    </tr>
                  ))
                )}
              </tbody>
            </table>
          </div>
        </>
      )}
    </div>
  )
}
