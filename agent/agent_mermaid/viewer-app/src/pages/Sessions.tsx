import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'

const API_BASE = ''

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

  useEffect(() => {
    fetch(`${API_BASE}/api/connections`)
      .then((r) => (r.ok ? r.json() : Promise.reject(new Error('Failed to load sessions'))))
      .then((data) => setSessions(data.sessions ?? []))
      .catch((e) => setError(e.message))
      .finally(() => setLoading(false))
  }, [])

  const now = Date.now() / 1000
  const isLive = (s: SessionRow) => (s.last_ts ?? 0) > now - 300

  return (
    <div className="page">
      <h2>Sessions</h2>
      <p>Live and past MCP sessions.</p>
      {loading && <p>Loading…</p>}
      {error && <p style={{ color: '#f87171' }}>{error}</p>}
      {!loading && !error && (
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Session ID</th>
                <th>Agent</th>
                <th>Status</th>
                <th>Start</th>
                <th>Duration</th>
                <th>Last message</th>
                <th></th>
              </tr>
            </thead>
            <tbody>
              {sessions.length === 0 ? (
                <tr>
                  <td colSpan={7}>No sessions yet.</td>
                </tr>
              ) : (
                sessions.map((s) => (
                  <tr key={s.session_id}>
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
                  </tr>
                ))
              )}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}
