import { useState } from 'react'
import type { ChangeEvent } from 'react'
import { apiFetch } from '../lib/api'

export interface UploadedRef {
  ref: string
  filename: string
}

interface PhotoPickerProps {
  uploads: UploadedRef[]
  onChange: (uploads: UploadedRef[]) => void
}

export function PhotoPicker({ uploads, onChange }: PhotoPickerProps) {
  const [error, setError] = useState<string | null>(null)

  async function handleFilesSelected(e: ChangeEvent<HTMLInputElement>) {
    const files = e.target.files
    if (!files || files.length === 0) return
    setError(null)
    const formData = new FormData()
    for (const file of Array.from(files)) {
      formData.append('files', file)
    }
    const res = await apiFetch('/v1/me/uploads', { method: 'POST', body: formData })
    if (!res.ok) {
      setError('upload failed')
      return
    }
    const body: { uploads: UploadedRef[] } = await res.json()
    onChange([...uploads, ...body.uploads])
    e.target.value = ''
  }

  return (
    <div>
      <label>
        Add photos
        <input type="file" accept="image/*" multiple onChange={handleFilesSelected} />
      </label>
      {error && <p role="alert">{error}</p>}
      <ul>
        {uploads.map((u) => (
          <li key={u.ref}>{u.filename}</li>
        ))}
      </ul>
    </div>
  )
}
