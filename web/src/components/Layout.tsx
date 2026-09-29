import { useCallback, useEffect, useState } from 'react'
import { NavLink, Outlet, useMatch } from 'react-router-dom'
import { api } from '../api/endpoints'
import type { Health } from '../api/types'
import { useAuth } from '../auth/AuthContext'
import { useInterval } from '../lib/hooks'
import { ROLE_LABEL } from '../lib/labels'
import { Icon } from './Icon'
import { ProcessWatcher } from './ProcessWatcher'

/** Молчит, пока всё хорошо; предупреждает, если модели или очередь недоступны. */
function SystemState() {
  const [health, setHealth] = useState<Health | null>(null)
  const [down, setDown] = useState(false)
  const check = useCallback(() => {
    api
      .health()
      .then((h) => {
        setHealth(h)
        setDown(false)
      })
      .catch(() => setDown(true))
  }, [])
  useEffect(check, [check])
  useInterval(check, 60_000, true)
  if (down) {
    return (
      <span className="sysstate sysstate--bad" title="GET /health не отвечает">
        <Icon name="alert" size={14} /> Сервер не отвечает
      </span>
    )
  }
  if (health?.llm && health.llm.reachable === false) {
    return (
      <span className="sysstate sysstate--warn" title={`Модель: ${health.llm.url ?? ''}`}>
        <Icon name="alert" size={14} /> Сервис моделей недоступен
      </span>
    )
  }
  return null
}

export function Layout() {
  const { user, logout, can } = useAuth()
  const bleed = useMatch('/processes/:id/findings/:fid')
  return (
    <div className="app">
      <header className="topbar">
        <NavLink to="/" className="brand" aria-label="Инспектор ИИ — к списку проверок">
          <span className="brand__mark" aria-hidden>
            <span />
          </span>
          <span className="brand__name">Инспектор ИИ</span>
        </NavLink>
        <nav className="nav" aria-label="Разделы">
          <NavLink to="/" end className={({ isActive }) => `nav__link${isActive ? ' nav__link--active' : ''}`}>
            Проверки
          </NavLink>
          <NavLink to="/params" className={({ isActive }) => `nav__link${isActive ? ' nav__link--active' : ''}`}>
            Нормативная база
          </NavLink>
          {can('gold') ? (
            <NavLink to="/gold" className={({ isActive }) => `nav__link${isActive ? ' nav__link--active' : ''}`}>
              GOLD и дообучение
            </NavLink>
          ) : null}
          {can('viewAudit') ? (
            <NavLink to="/audit" className={({ isActive }) => `nav__link${isActive ? ' nav__link--active' : ''}`}>
              Журнал
            </NavLink>
          ) : null}
        </nav>
        <div className="topbar__right">
          <SystemState />
          {user ? (
            <span className="whoami" title={user.login}>
              <span className="whoami__name">{user.full_name || user.login}</span>
              <span className="whoami__role">{ROLE_LABEL[user.role] ?? user.role}</span>
            </span>
          ) : null}
          <button type="button" className="btn btn--ghost btn--small" onClick={logout}>
            Выйти
          </button>
        </div>
      </header>
      <ProcessWatcher />
      <main className={bleed ? 'main main--bleed' : 'main'}>
        <Outlet />
      </main>
    </div>
  )
}
