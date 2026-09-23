import { Link } from 'react-router-dom'
import { useAuth } from '../context/AuthContext'
import './Home.css'

interface Action {
  to: string
  title: string
  desc: string
  icon: string
  adminOnly?: boolean
}

const ACTIONS: Action[] = [
  {
    to: '/submit',
    title: 'Submit an item',
    desc: 'Upload photos and details to list a new item for intake.',
    icon: '📷',
  },
  {
    to: '/submissions',
    title: 'My submissions',
    desc: 'Track status, review outcomes, and resubmit when info is needed.',
    icon: '📋',
  },
  {
    to: '/admin',
    title: 'Admin Console',
    desc: 'Configure providers, the sufficiency gate, and review all submissions.',
    icon: '⚙️',
    adminOnly: true,
  },
]

export function Home() {
  const { user } = useAuth()
  const actions = ACTIONS.filter((a) => !a.adminOnly || user?.role === 'admin')

  return (
    <div className="home">
      <section className="home__hero">
        <p className="home__eyebrow">Acme Resale Intake</p>
        <h1>Turn photos into ready-to-list products.</h1>
        <p className="home__sub">
          Submit an item and let the pipeline identify it, grade condition, and draft the listing —
          then track every submission to publish.
        </p>
        <div className="home__cta">
          <Link to="/submit" className="btn btn--primary">
            Submit an item
          </Link>
          <Link to="/submissions" className="btn btn--ghost">
            View my submissions
          </Link>
        </div>
      </section>

      <section className="home__cards">
        {actions.map((a) => (
          <Link key={a.to} to={a.to} className="action-card">
            <span className="action-card__icon" aria-hidden="true">
              {a.icon}
            </span>
            <span className="action-card__body">
              <span className="action-card__title">{a.title}</span>
              <span className="action-card__desc">{a.desc}</span>
            </span>
            <span className="action-card__arrow" aria-hidden="true">
              →
            </span>
          </Link>
        ))}
      </section>
    </div>
  )
}
