import { useState } from 'react'
import { ProvidersTab } from './ProvidersTab'
import { GateFieldsTab } from './GateFieldsTab'
import { BusinessConfigTab } from './BusinessConfigTab'
import { SubmissionsTab } from './SubmissionsTab'
import './AdminConsole.css'

type Tab = 'providers' | 'gate' | 'business' | 'submissions'

const TABS: { id: Tab; label: string }[] = [
  { id: 'providers', label: 'Providers' },
  { id: 'gate', label: 'Gate & Fields' },
  { id: 'business', label: 'Business Config' },
  { id: 'submissions', label: 'Submissions' },
]

export function AdminConsole() {
  const [active, setActive] = useState<Tab>('providers')

  return (
    <div className="admin">
      <header className="admin__head">
        <h1>Admin Console</h1>
        <p className="admin__sub">Manage the intake pipeline and review submissions.</p>
      </header>
      <div className="admin__tabs" role="tablist">
        {TABS.map((tab) => (
          <button
            key={tab.id}
            className="admin__tab"
            role="tab"
            aria-selected={active === tab.id}
            onClick={() => setActive(tab.id)}
          >
            {tab.label}
          </button>
        ))}
      </div>
      <div className="admin__panel card" role="tabpanel">
        {active === 'providers' && <ProvidersTab />}
        {active === 'gate' && <GateFieldsTab />}
        {active === 'business' && <BusinessConfigTab />}
        {active === 'submissions' && <SubmissionsTab />}
      </div>
    </div>
  )
}
