import { Routes, Route } from 'react-router-dom'
import { RequireAuth } from './components/RequireAuth'
import { RequireAdmin } from './components/RequireAdmin'
import { AppShell } from './components/AppShell'
import { Login } from './pages/Login'
import { Signup } from './pages/Signup'
import { Home } from './pages/Home'
import { AdminConsole } from './pages/admin/AdminConsole'
import { Submit } from './pages/Submit'
import { Submissions } from './pages/Submissions'
import { SubmissionResult } from './pages/SubmissionResult'

export function App() {
  return (
    <Routes>
      <Route path="/login" element={<Login />} />
      <Route path="/signup" element={<Signup />} />
      <Route
        path="/"
        element={
          <RequireAuth>
            <AppShell>
              <Home />
            </AppShell>
          </RequireAuth>
        }
      />
      <Route
        path="/admin"
        element={
          <RequireAdmin>
            <AppShell>
              <AdminConsole />
            </AppShell>
          </RequireAdmin>
        }
      />
      <Route
        path="/submit"
        element={
          <RequireAuth>
            <AppShell>
              <Submit />
            </AppShell>
          </RequireAuth>
        }
      />
      <Route
        path="/submissions"
        element={
          <RequireAuth>
            <AppShell>
              <Submissions />
            </AppShell>
          </RequireAuth>
        }
      />
      <Route
        path="/submissions/:id"
        element={
          <RequireAuth>
            <AppShell>
              <SubmissionResult />
            </AppShell>
          </RequireAuth>
        }
      />
    </Routes>
  )
}
