import { useEffect, useState } from 'react'
import type { FormEvent } from 'react'
import { deleteAdminConfig, fetchAdminConfig, updateAdminConfig } from '../../lib/adminApi'
import type { AdminConfigEntry } from '../../lib/adminApi'

function toCsv(value: unknown): string {
  return Array.isArray(value) ? value.join(', ') : ''
}

function fromCsv(value: string): string[] {
  return value.split(',').map((s) => s.trim()).filter(Boolean)
}

export function GateFieldsTab() {
  const [entries, setEntries] = useState<Record<string, AdminConfigEntry>>({})
  const [threshold, setThreshold] = useState('')
  const [mandatoryFields, setMandatoryFields] = useState('')
  const [coreFields, setCoreFields] = useState('')
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  async function load() {
    try {
      const config = await fetchAdminConfig()
      const byKey = Object.fromEntries(config.map((e) => [e.key, e]))
      setEntries(byKey)
      setThreshold(String(byKey['gate.threshold']?.value ?? ''))
      setMandatoryFields(toCsv(byKey['gate.mandatory_fields']?.value))
      setCoreFields(toCsv(byKey['gate.core_fields']?.value))
    } catch (err) {
      setError(err instanceof Error ? err.message : 'failed to load config')
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    load()
  }, [])

  async function save(key: string, value: unknown, e: FormEvent) {
    e.preventDefault()
    setError(null)
    try {
      await updateAdminConfig(key, value)
      await load()
    } catch (err) {
      setError(err instanceof Error ? err.message : 'failed to save')
    }
  }

  async function reset(key: string) {
    setError(null)
    try {
      await deleteAdminConfig(key)
      await load()
    } catch (err) {
      setError(err instanceof Error ? err.message : 'failed to reset')
    }
  }

  if (loading) {
    return <p>Loading...</p>
  }

  return (
    <div>
      <h2>Gate & Fields</h2>
      {error && <p role="alert">{error}</p>}

      <form onSubmit={(e) => save('gate.threshold', Number(threshold), e)}>
        <label htmlFor="gate-threshold">Gate threshold</label>
        <input
          id="gate-threshold" type="number" step="0.01" min="0" max="1"
          value={threshold} onChange={(e) => setThreshold(e.target.value)}
        />
        <button type="submit">Save threshold</button>
        {entries['gate.threshold']?.is_override && (
          <button type="button" onClick={() => reset('gate.threshold')}>Reset to default</button>
        )}
      </form>

      <form onSubmit={(e) => save('gate.mandatory_fields', fromCsv(mandatoryFields), e)}>
        <label htmlFor="mandatory-fields">Mandatory fields (comma-separated)</label>
        <textarea
          id="mandatory-fields" value={mandatoryFields}
          onChange={(e) => setMandatoryFields(e.target.value)}
        />
        <button type="submit">Save mandatory fields</button>
        {entries['gate.mandatory_fields']?.is_override && (
          <button type="button" onClick={() => reset('gate.mandatory_fields')}>Reset to default</button>
        )}
      </form>

      <form onSubmit={(e) => save('gate.core_fields', fromCsv(coreFields), e)}>
        <label htmlFor="core-fields">Core fields (comma-separated)</label>
        <textarea
          id="core-fields" value={coreFields}
          onChange={(e) => setCoreFields(e.target.value)}
        />
        <button type="submit">Save core fields</button>
        {entries['gate.core_fields']?.is_override && (
          <button type="button" onClick={() => reset('gate.core_fields')}>Reset to default</button>
        )}
      </form>
    </div>
  )
}
