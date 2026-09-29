import type { ReactNode } from 'react'
import { Navigate, Route, Routes, useLocation } from 'react-router-dom'
import { useAuth } from './auth/AuthContext'
import type { Action } from './auth/permissions'
import { Empty, Loading } from './components/Feedback'
import { Layout } from './components/Layout'
import { AuditPage } from './pages/AuditPage'
import { DashboardPage } from './pages/DashboardPage'
import { GoldPage } from './pages/GoldPage'
import { LoginPage } from './pages/LoginPage'
import { ParamsPage } from './pages/ParamsPage'
import { ProcessPage } from './pages/ProcessPage'
import { UploadPage } from './pages/UploadPage'
import { WorkbenchPage } from './pages/WorkbenchPage'

function RequireAuth({ children }: { children: ReactNode }) {
  const { user, checking } = useAuth()
  const location = useLocation()
  if (checking) return <Loading text="Проверяем вход…" />
  if (!user) {
    const next = location.pathname + location.search
    return <Navigate to={next && next !== '/' ? `/login?next=${encodeURIComponent(next)}` : '/login'} replace />
  }
  return <>{children}</>
}

function RequireRole({ action, children }: { action: Action; children: ReactNode }) {
  const { can } = useAuth()
  if (!can(action)) {
    return (
      <div className="page">
        <Empty title="Раздел недоступен для вашей роли">
          Обратитесь к администратору, если доступ нужен для работы.
        </Empty>
      </div>
    )
  }
  return <>{children}</>
}

function NotFound() {
  return (
    <div className="page">
      <Empty title="Такой страницы нет">Проверьте адрес или вернитесь к списку проверок.</Empty>
    </div>
  )
}

export function App() {
  return (
    <Routes>
      <Route path="/login" element={<LoginPage />} />
      <Route
        element={
          <RequireAuth>
            <Layout />
          </RequireAuth>
        }
      >
        <Route index element={<DashboardPage />} />
        <Route
          path="processes/new"
          element={
            <RequireRole action="upload">
              <UploadPage />
            </RequireRole>
          }
        />
        <Route path="processes/:id" element={<ProcessPage />} />
        <Route
          path="processes/:id/upload"
          element={
            <RequireRole action="upload">
              <UploadPage />
            </RequireRole>
          }
        />
        <Route path="processes/:id/findings/:fid" element={<WorkbenchPage />} />
        <Route path="params" element={<ParamsPage />} />
        <Route
          path="gold"
          element={
            <RequireRole action="gold">
              <GoldPage />
            </RequireRole>
          }
        />
        <Route
          path="audit"
          element={
            <RequireRole action="viewAudit">
              <AuditPage />
            </RequireRole>
          }
        />
        <Route path="*" element={<NotFound />} />
      </Route>
    </Routes>
  )
}
