import { Fragment, useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { apiFetch } from '../lib/api'
import './Submissions.css'

interface SubmissionSummary {
  submission_id: string
  state: string
  outcome: string | null
  product_key: string | null
  listing_sku: string | null
  cost_cad: number | null
  missing: string[]
}

interface Thread {
  root: SubmissionSummary
  resubmissions: SubmissionSummary[]
}

type BadgeKind = 'success' | 'warn' | 'danger' | 'progress' | 'neutral'

function badgeKind(s: SubmissionSummary): BadgeKind {
  const label = (s.outcome ?? s.state).toLowerCase()
  if (label.includes('reject')) return 'danger'
  if (s.missing.length > 0 || label.includes('unknown') || label.includes('clarif')) return 'warn'
  if (label.includes('accept') || label.includes('publish')) return 'success'
  if (label.includes('receiv') || label.includes('ingest')) return 'progress'
  return 'neutral'
}

function StatusBadge({ s }: { s: SubmissionSummary }) {
  return <span className={`badge badge--${badgeKind(s)}`}>{s.outcome ?? s.state}</span>
}

function formatCost(cost: number | null): string {
  if (cost == null) return '—'
  return `$${cost.toFixed(2)}`
}

export function Submissions() {
  const [threads, setThreads] = useState<Thread[]>([])
  const [expanded, setExpanded] = useState<Record<string, boolean>>({})
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    apiFetch('/v1/me/submissions')
      .then(async (res) => {
        if (!res.ok) {
          throw new Error(`Couldn't load your submissions (${res.status}).`)
        }
        const body: { threads: Thread[] } = await res.json()
        setThreads(body.threads)
      })
      .catch((err) => setError(err instanceof Error ? err.message : 'Something went wrong.'))
      .finally(() => setLoading(false))
  }, [])

  function toggle(id: string) {
    setExpanded((prev) => ({ ...prev, [id]: !prev[id] }))
  }

  return (
    <div className="submissions">
      <div className="submissions__head">
        <h1>My submissions</h1>
        <Link className="submissions__new" to="/submit">
          + Submit a new item
        </Link>
      </div>

      {loading && <p className="submissions__muted">Loading…</p>}

      {!loading && error && (
        <p className="submissions__error" role="alert">
          {error}
        </p>
      )}

      {!loading && !error && threads.length === 0 && (
        <div className="submissions__empty">
          <p>You haven’t submitted any items yet.</p>
          <p>
            <Link to="/submit">Submit your first item →</Link>
          </p>
        </div>
      )}

      {!loading && !error && threads.length > 0 && (
        <div className="submissions__scroll">
          <table className="submissions__table">
            <thead>
              <tr>
                <th>Status</th>
                <th>Item</th>
                <th>SKU</th>
                <th>Cost</th>
                <th>History</th>
              </tr>
            </thead>
            <tbody>
              {threads.map((thread) => {
                const latest =
                  thread.resubmissions.length > 0 ? thread.resubmissions[0] : thread.root
                const hasHistory = thread.resubmissions.length > 0
                const isOpen = expanded[thread.root.submission_id]
                return (
                  <Fragment key={thread.root.submission_id}>
                    <tr className="submissions__row">
                      <td>
                        <StatusBadge s={latest} />
                        {latest.missing.length > 0 && (
                          <div className="submissions__needs">
                            Needs: {latest.missing.join(', ')}
                          </div>
                        )}
                      </td>
                      <td>
                        <Link
                          className="submissions__item-link"
                          to={`/submissions/${latest.submission_id}`}
                        >
                          {latest.product_key ?? 'Untitled item'}
                        </Link>
                        <span className="submissions__id">{latest.submission_id}</span>
                      </td>
                      <td className="submissions__muted">{latest.listing_sku ?? '—'}</td>
                      <td className="submissions__cost">{formatCost(latest.cost_cad)}</td>
                      <td>
                        {hasHistory ? (
                          <button
                            type="button"
                            className="submissions__history-btn"
                            onClick={() => toggle(thread.root.submission_id)}
                          >
                            {isOpen ? 'Hide' : `${thread.resubmissions.length + 1} versions`}
                          </button>
                        ) : (
                          <span className="submissions__muted">—</span>
                        )}
                      </td>
                    </tr>
                    {hasHistory && isOpen && (
                      <tr className="submissions__history">
                        <td colSpan={5}>
                          <ul className="submissions__history-list">
                            <li>
                              <Link to={`/submissions/${thread.root.submission_id}`}>
                                <StatusBadge s={thread.root} /> Original ·{' '}
                                {thread.root.submission_id}
                              </Link>
                            </li>
                            {thread.resubmissions.map((r) => (
                              <li key={r.submission_id}>
                                <Link to={`/submissions/${r.submission_id}`}>
                                  <StatusBadge s={r} /> {r.submission_id}
                                </Link>
                              </li>
                            ))}
                          </ul>
                        </td>
                      </tr>
                    )}
                  </Fragment>
                )
              })}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}
