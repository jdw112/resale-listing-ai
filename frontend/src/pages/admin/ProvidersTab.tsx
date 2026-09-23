import { useEffect, useState } from 'react'
import { deleteAdminConfig, fetchAdminConfig, updateAdminConfig } from '../../lib/adminApi'
import type { AdminConfigEntry } from '../../lib/adminApi'

const SEAMS: { key: string; label: string; options: string[] }[] = [
  { key: 'provider_stack.vision_provider', label: 'Vision provider', options: ['openai', 'gemini', 'openrouter', 'bedrock'] },
  { key: 'provider_stack.copy_provider', label: 'Copy provider', options: ['openai', 'gemini', 'openrouter'] },
  { key: 'provider_stack.web_search_provider', label: 'Web search provider', options: ['openai_web_search', 'perplexity_sonar', 'gemini_search', 'openrouter_online'] },
  { key: 'provider_stack.image_process_provider', label: 'Image process provider', options: ['photoroom_clipdrop', 'gemini', 'bedrock'] },
  { key: 'provider_stack.json_repair_provider', label: 'JSON repair provider', options: ['claude', 'openai'] },
]

export function ProvidersTab() {
  const [entries, setEntries] = useState<Record<string, AdminConfigEntry>>({})
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  async function load() {
    try {
      const config = await fetchAdminConfig()
      setEntries(Object.fromEntries(config.map((e) => [e.key, e])))
    } catch (err) {
      setError(err instanceof Error ? err.message : 'failed to load config')
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    load()
  }, [])

  async function handleChange(key: string, value: string) {
    setError(null)
    try {
      await updateAdminConfig(key, value)
      await load()
    } catch (err) {
      setError(err instanceof Error ? err.message : 'failed to save')
    }
  }

  async function handleReset(key: string) {
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
      <h2>Providers</h2>
      {error && <p role="alert">{error}</p>}
      {SEAMS.map((seam) => {
        const entry = entries[seam.key]
        if (!entry) return null
        return (
          <div key={seam.key}>
            <label htmlFor={seam.key}>{seam.label}</label>
            <select
              id={seam.key}
              value={entry.value as string}
              onChange={(e) => handleChange(seam.key, e.target.value)}
            >
              {seam.options.map((opt) => (
                <option key={opt} value={opt}>{opt}</option>
              ))}
            </select>
            {entry.is_override && (
              <span>
                {' '}overridden (default: {String(entry.default)}){' '}
                <button type="button" onClick={() => handleReset(seam.key)}>Reset to default</button>
              </span>
            )}
          </div>
        )
      })}
    </div>
  )
}
