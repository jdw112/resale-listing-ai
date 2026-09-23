import { useEffect, useState } from 'react'
import type { FormEvent } from 'react'
import { deleteAdminConfig, fetchAdminConfig, updateAdminConfig } from '../../lib/adminApi'
import type { AdminConfigEntry } from '../../lib/adminApi'

export function BusinessConfigTab() {
  const [entries, setEntries] = useState<Record<string, AdminConfigEntry>>({})
  const [longestSide, setLongestSide] = useState('')
  const [lengthPlusGirth, setLengthPlusGirth] = useState('')
  const [weight, setWeight] = useState('')
  const [categoriesJson, setCategoriesJson] = useState('')
  const [conditionScaleJson, setConditionScaleJson] = useState('')
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  async function load() {
    try {
      const config = await fetchAdminConfig()
      const byKey = Object.fromEntries(config.map((e) => [e.key, e]))
      setEntries(byKey)
      const oversized = byKey['business.oversized_ups_ca']?.value as
        { longest_side_cm: number; length_plus_girth_cm: number; weight_kg: number } | undefined
      setLongestSide(String(oversized?.longest_side_cm ?? ''))
      setLengthPlusGirth(String(oversized?.length_plus_girth_cm ?? ''))
      setWeight(String(oversized?.weight_kg ?? ''))
      setCategoriesJson(JSON.stringify(byKey['business.categories']?.value ?? {}, null, 2))
      setConditionScaleJson(JSON.stringify(byKey['business.condition_scale']?.value ?? {}, null, 2))
    } catch (err) {
      setError(err instanceof Error ? err.message : 'failed to load config')
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    load()
  }, [])

  async function saveOversized(e: FormEvent) {
    e.preventDefault()
    setError(null)
    try {
      await updateAdminConfig('business.oversized_ups_ca', {
        longest_side_cm: Number(longestSide),
        length_plus_girth_cm: Number(lengthPlusGirth),
        weight_kg: Number(weight),
      })
      await load()
    } catch (err) {
      setError(err instanceof Error ? err.message : 'failed to save')
    }
  }

  async function saveJson(key: string, raw: string, e: FormEvent) {
    e.preventDefault()
    setError(null)
    try {
      await updateAdminConfig(key, JSON.parse(raw))
      await load()
    } catch (err) {
      setError(err instanceof Error ? err.message : `invalid JSON or save failed: ${err instanceof Error ? err.message : ''}`)
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
      <h2>Business Config</h2>
      {error && <p role="alert">{error}</p>}

      <form onSubmit={saveOversized}>
        <h3>Oversized package rules (UPS Canada)</h3>
        <label htmlFor="longest-side">Longest side (cm)</label>
        <input id="longest-side" type="number" step="0.01" value={longestSide}
               onChange={(e) => setLongestSide(e.target.value)} />
        <label htmlFor="length-plus-girth">Length + girth (cm)</label>
        <input id="length-plus-girth" type="number" step="0.01" value={lengthPlusGirth}
               onChange={(e) => setLengthPlusGirth(e.target.value)} />
        <label htmlFor="weight-kg">Weight (kg)</label>
        <input id="weight-kg" type="number" step="0.01" value={weight}
               onChange={(e) => setWeight(e.target.value)} />
        <button type="submit">Save oversized rules</button>
        {entries['business.oversized_ups_ca']?.is_override && (
          <button type="button" onClick={() => reset('business.oversized_ups_ca')}>Reset to default</button>
        )}
      </form>

      <form onSubmit={(e) => saveJson('business.categories', categoriesJson, e)}>
        <h3>Categories</h3>
        <label htmlFor="categories-json">Categories (JSON)</label>
        <textarea id="categories-json" rows={8} value={categoriesJson}
                  onChange={(e) => setCategoriesJson(e.target.value)} />
        <button type="submit">Save categories</button>
        {entries['business.categories']?.is_override && (
          <button type="button" onClick={() => reset('business.categories')}>Reset to default</button>
        )}
      </form>

      <form onSubmit={(e) => saveJson('business.condition_scale', conditionScaleJson, e)}>
        <h3>Condition scale</h3>
        <label htmlFor="condition-scale-json">Condition scale (JSON)</label>
        <textarea id="condition-scale-json" rows={6} value={conditionScaleJson}
                  onChange={(e) => setConditionScaleJson(e.target.value)} />
        <button type="submit">Save condition scale</button>
        {entries['business.condition_scale']?.is_override && (
          <button type="button" onClick={() => reset('business.condition_scale')}>Reset to default</button>
        )}
      </form>
    </div>
  )
}
