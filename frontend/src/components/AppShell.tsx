import type { ReactNode } from 'react'
import { Link, NavLink, useNavigate } from 'react-router-dom'
import { useAuth } from '../context/AuthContext'
import './AppShell.css'

export function AppShell({ children }: { children: ReactNode }) {
  const { user, logout } = useAuth()
  const navigate = useNavigate()

  async function handleLogout() {
    await logout()
    navigate('/login')
  }

  return (
    <>
      <header className="nav">
        <div className="nav__inner">
          <Link to="/" className="nav__brand">
            Acme Resale
          </Link>
          <nav className="nav__links">
            <NavLink to="/submit" className="nav__link">
              Submit
            </NavLink>
            <NavLink to="/submissions" className="nav__link">
              My submissions
            </NavLink>
            {user?.role === 'admin' && (
              <NavLink to="/admin" className="nav__link">
                Admin
              </NavLink>
            )}
          </nav>
          <div className="nav__user">
            {user && <span className="nav__email">{user.email}</span>}
            <button type="button" className="btn btn--ghost nav__logout" onClick={handleLogout}>
              Log out
            </button>
          </div>
        </div>
      </header>
      <main className="container">{children}</main>
    </>
  )
}
