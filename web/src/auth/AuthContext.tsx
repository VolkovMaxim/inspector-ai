import { createContext, useCallback, useContext, useEffect, useMemo, useState, type ReactNode } from 'react'
import { useLocation, useNavigate } from 'react-router-dom'
import { clearToken, getToken, isAbort, setToken, setUnauthorizedHandler } from '../api/client'
import { api } from '../api/endpoints'
import type { User } from '../api/types'
import { can, type Action } from './permissions'

interface AuthState {
  user: User | null
  /** true, пока проверяем сохранённый токен при старте */
  checking: boolean
  login: (login: string, password: string) => Promise<User>
  logout: () => void
  can: (action: Action) => boolean
}

const AuthContext = createContext<AuthState | null>(null)

export function AuthProvider({ children }: { children: ReactNode }) {
  const [user, setUser] = useState<User | null>(null)
  const [checking, setChecking] = useState<boolean>(() => !!getToken())
  const navigate = useNavigate()
  const location = useLocation()

  // Проверить сохранённый токен
  useEffect(() => {
    if (!getToken()) {
      setChecking(false)
      return
    }
    const ctrl = new AbortController()
    api
      .me(ctrl.signal)
      .then((u) => setUser(u))
      .catch((e) => {
        if (isAbort(e)) return
        clearToken()
        setUser(null)
      })
      .finally(() => {
        if (!ctrl.signal.aborted) setChecking(false)
      })
    return () => ctrl.abort()
  }, [])

  // 401 из любого запроса → на вход, с возвратом туда, где был
  const here = location.pathname + location.search
  useEffect(() => {
    setUnauthorizedHandler(() => {
      setUser(null)
      if (!here.startsWith('/login')) navigate(`/login?next=${encodeURIComponent(here)}`, { replace: true })
    })
    return () => setUnauthorizedHandler(null)
  }, [navigate, here])

  const login = useCallback(async (loginName: string, password: string) => {
    const res = await api.login(loginName, password)
    setToken(res.access_token)
    setUser(res.user)
    return res.user
  }, [])

  const logout = useCallback(() => {
    clearToken()
    setUser(null)
    navigate('/login', { replace: true })
  }, [navigate])

  const value = useMemo<AuthState>(
    () => ({ user, checking, login, logout, can: (action: Action) => can(user?.role, action) }),
    [user, checking, login, logout],
  )
  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>
}

export function useAuth(): AuthState {
  const ctx = useContext(AuthContext)
  if (!ctx) throw new Error('useAuth вне AuthProvider')
  return ctx
}
