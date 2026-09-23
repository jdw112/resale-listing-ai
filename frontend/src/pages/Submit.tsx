import { useEffect, useState } from 'react'
import type { FormEvent } from 'react'
import { useNavigate } from 'react-router-dom'
import { apiFetch } from '../lib/api'
import { PhotoPicker } from '../components/PhotoPicker'
import type { UploadedRef } from '../components/PhotoPicker'
import './Submit.css'

export function Submit() {
  const navigate = useNavigate()
  const [uploads, setUploads] = useState<UploadedRef[]>([])
  const [grades, setGrades] = useState<string[]>([])
  const [conditionGrade, setConditionGrade] = useState('')
  const [notes, setNotes] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [pending, setPending] = useState(false)

  useEffect(() => {
    apiFetch('/v1/me/config/condition-grades')
      .then((res) => (res.ok ? res.json() : { grades: [] }))
      .then((body: { grades: string[] }) => setGrades(body.grades))
  }, [])

  async function handleSubmit(e: FormEvent) {
    e.preventDefault()
    setError(null)
    setPending(true)
    try {
      const res = await apiFetch('/v1/me/submissions', {
        method: 'POST',
        body: JSON.stringify({
          images: uploads.map((u) => u.ref),
          notes: notes || null,
          provided_condition_grade: conditionGrade || null,
        }),
      })
      if (!res.ok) {
        const body = await res.json()
        throw new Error(body.detail ?? 'submission failed')
      }
      const body: { submission_id: string } = await res.json()
      navigate(`/submissions/${body.submission_id}`)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'submission failed')
    } finally {
      setPending(false)
    }
  }

  return (
    <div className="submit">
      <header className="submit__head">
        <h1>Submit an item</h1>
        <p className="submit__sub">
          Add a few photos, then optionally note the condition and anything the pipeline should know.
        </p>
      </header>
      <form className="card submit__form" onSubmit={handleSubmit}>
        <div className="field">
          <span className="field-label">Photos</span>
          <PhotoPicker uploads={uploads} onChange={setUploads} />
          <span className="field-hint">At least one photo is required.</span>
        </div>

        <div className="field">
          <label htmlFor="condition">Condition</label>
          <select
            id="condition"
            value={conditionGrade}
            onChange={(e) => setConditionGrade(e.target.value)}
          >
            <option value="">Select a condition (optional)</option>
            {grades.map((g) => (
              <option key={g} value={g}>
                {g}
              </option>
            ))}
          </select>
        </div>

        <div className="field">
          <label htmlFor="notes">Notes</label>
          <textarea
            id="notes"
            value={notes}
            onChange={(e) => setNotes(e.target.value)}
            placeholder="Optional — anything notable about the item."
          />
        </div>

        {error && (
          <p className="form-error" role="alert">
            {error}
          </p>
        )}
        <button type="submit" className="btn btn--primary" disabled={pending || uploads.length === 0}>
          {pending ? 'Submitting…' : 'Submit item'}
        </button>
      </form>
    </div>
  )
}
