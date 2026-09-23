import { useCallback, useEffect, useRef, useState } from 'react'
import { useParams, Link } from 'react-router-dom'
import { apiFetch } from '../lib/api'
import { PhotoPicker } from '../components/PhotoPicker'
import type { UploadedRef } from '../components/PhotoPicker'
import './SubmissionResult.css'

interface SubmissionDetail {
  submission_id: string
  state: string
  outcome: string | null
  missing: string[]
  product: Record<string, { value: unknown }> | null
  listing: Record<string, { value: unknown }> | null
}

const POLL_MS = 3000

// While outcome is null the pipeline is still working (RECEIVED / INGESTED);
// once an outcome lands (Accepted / Rejected / Clarification required) it's terminal.
function isProcessing(detail: SubmissionDetail | null): boolean {
  return detail != null && detail.outcome === null
}

function statusKind(detail: SubmissionDetail): string {
  const label = (detail.outcome ?? detail.state).toLowerCase()
  if (label.includes('reject')) return 'danger'
  if (detail.missing.length > 0 || label.includes('clarif') || label.includes('unknown')) return 'warn'
  if (label.includes('accept') || label.includes('publish')) return 'success'
  return 'progress'
}

export function SubmissionResult() {
  const { id } = useParams<{ id: string }>()
  const [detail, setDetail] = useState<SubmissionDetail | null>(null)
  const [loading, setLoading] = useState(true)
  const [notFound, setNotFound] = useState(false)
  const [answers, setAnswers] = useState<Record<string, string>>({})
  const [moreUploads, setMoreUploads] = useState<UploadedRef[]>([])
  const [error, setError] = useState<string | null>(null)
  const [pending, setPending] = useState(false)
  const [answered, setAnswered] = useState(false)
  const timer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined)
  const alive = useRef(true)

  const poll = useCallback(
    async function poll() {
      const res = await apiFetch(`/v1/me/submissions/${id}`)
      if (!alive.current) return
      if (!res.ok) {
        setNotFound(true)
        setLoading(false)
        return
      }
      const data: SubmissionDetail = await res.json()
      if (!alive.current) return
      setDetail(data)
      setLoading(false)
      if (isProcessing(data)) {
        timer.current = setTimeout(poll, POLL_MS)
      }
    },
    [id],
  )

  useEffect(() => {
    alive.current = true
    poll()
    return () => {
      alive.current = false
      clearTimeout(timer.current)
    }
  }, [poll])

  async function handleAnswerSubmit() {
    setError(null)
    setPending(true)
    try {
      const fieldOverrides = Object.fromEntries(
        Object.entries(answers).filter(([, value]) => value.trim() !== ''),
      )
      const res = await apiFetch(`/v1/me/submissions/${id}/answer`, {
        method: 'POST',
        body: JSON.stringify({
          field_overrides: fieldOverrides,
          images: moreUploads.map((u) => u.ref),
        }),
      })
      if (!res.ok) {
        const body = await res.json()
        throw new Error(body.detail ?? 'answer failed')
      }
      setAnswered(true)
      // Reprocessing kicks off — resume live polling for the new outcome.
      setDetail((d) => (d ? { ...d, outcome: null, state: 'RECEIVED' } : d))
      poll()
    } catch (err) {
      setError(err instanceof Error ? err.message : 'answer failed')
    } finally {
      setPending(false)
    }
  }

  if (loading) {
    return (
      <div className="result">
        <p className="result__muted">Loading…</p>
      </div>
    )
  }
  if (notFound || !detail) {
    return (
      <div className="result">
        <p className="result__muted">Submission not found.</p>
        <p>
          <Link to="/submissions">← Back to my submissions</Link>
        </p>
      </div>
    )
  }

  const processing = isProcessing(detail)

  return (
    <div className="result">
      <p className="result__back">
        <Link to="/submissions">← My submissions</Link>
      </p>
      <header className="result__head">
        <h1>Submission</h1>
        <code className="result__id">{detail.submission_id}</code>
      </header>

      <div className="result__status">
        {processing ? (
          <span className="result__processing">
            <span className="spinner" aria-hidden="true" />
            Processing — {detail.state.toLowerCase()}…
          </span>
        ) : (
          <span className={`badge badge--${statusKind(detail)}`}>
            {detail.outcome ?? detail.state}
          </span>
        )}
      </div>
      {processing && (
        <p className="result__muted">
          This updates automatically — no need to refresh. It usually takes a moment.
        </p>
      )}

      {detail.outcome === 'Clarification required' && !answered && (
        <div className="card result__answer">
          <h2>A few more details needed</h2>
          {detail.missing.map((field) => (
            <div className="field" key={field}>
              <label htmlFor={`ans-${field}`}>{field}</label>
              <input
                id={`ans-${field}`}
                type="text"
                value={answers[field] ?? ''}
                onChange={(e) => setAnswers((prev) => ({ ...prev, [field]: e.target.value }))}
              />
            </div>
          ))}
          <div className="field">
            <span className="field-label">Add more photos (optional)</span>
            <PhotoPicker uploads={moreUploads} onChange={setMoreUploads} />
          </div>
          {error && (
            <p className="form-error" role="alert">
              {error}
            </p>
          )}
          <button type="button" className="btn btn--primary" disabled={pending} onClick={handleAnswerSubmit}>
            {pending ? 'Submitting…' : 'Submit answer'}
          </button>
        </div>
      )}

      {answered && <p className="result__muted">Thanks — reprocessing your answer.</p>}

      {detail.product && (
        <section className="card result__section">
          <h2>Product</h2>
          <dl className="result__fields">
            {Object.entries(detail.product).map(([field, env]) => (
              <div className="result__field" key={field}>
                <dt>{field}</dt>
                <dd>{String(env.value)}</dd>
              </div>
            ))}
          </dl>
        </section>
      )}

      {detail.listing && (
        <section className="card result__section">
          <h2>Listing</h2>
          <dl className="result__fields">
            {Object.entries(detail.listing).map(([field, env]) => (
              <div className="result__field" key={field}>
                <dt>{field}</dt>
                <dd>{String(env.value)}</dd>
              </div>
            ))}
          </dl>
        </section>
      )}
    </div>
  )
}
