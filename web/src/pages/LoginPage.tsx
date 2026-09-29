import { useState, type FormEvent } from 'react'
import { Navigate, useNavigate, useSearchParams } from 'react-router-dom'
import { ApiError, errorText } from '../api/client'
import { useAuth } from '../auth/AuthContext'
import { Field } from '../components/Feedback'
import { Stamp } from '../components/Stamp'
import { useDocumentTitle } from '../lib/hooks'


function safeNext(raw: string | null): string {
  // только относительные пути внутри приложения — без открытых редиректов
  if (!raw || !raw.startsWith('/') || raw.startsWith('//') || raw.startsWith('/login')) return '/'
  return raw
}

export function LoginPage() {
  useDocumentTitle('Вход')
  const { user, login } = useAuth()
  const [params] = useSearchParams()
  const navigate = useNavigate()
  const [loginName, setLoginName] = useState('')
  const [password, setPassword] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const next = safeNext(params.get('next'))

  if (user) return <Navigate to={next} replace />

  const submit = async (e: FormEvent) => {
    e.preventDefault()
    if (!loginName.trim() || !password) {
      setError('Введите логин и пароль')
      return
    }
    setBusy(true)
    setError(null)
    try {
      await login(loginName.trim(), password)
      navigate(next, { replace: true })
    } catch (err) {
      if (err instanceof ApiError && (err.status === 401 || err.code === 'AUTH_FAILED')) {
        setError('Неверный логин или пароль')
      } else {
        setError(errorText(err))
      }
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="login">
      <form className="login__sheet" onSubmit={submit} noValidate>
        <div className="login__body">
          <h1>
            <span className="brand__mark" aria-hidden>
              <span />
            </span>
            Инспектор ИИ
          </h1>
          <p className="login__lead">Сверка проектной, рабочей и исполнительной документации объекта</p>
          <Field label="Логин">
            <input
              className="input"
              name="username"
              autoComplete="username"
              autoFocus
              value={loginName}
              onChange={(e) => setLoginName(e.target.value)}
            />
          </Field>
          <Field label="Пароль">
            <input
              className="input"
              type="password"
              name="password"
              autoComplete="current-password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
            />
          </Field>
          {error ? (
            <p className="login__error" role="alert">
              {error}
            </p>
          ) : null}
          <button type="submit" className="btn btn--primary" disabled={busy}>
            {busy ? 'Входим…' : 'Войти'}
          </button>
          {/* стенд хакатона: демо-учётки видны всегда (пароли по умолчанию — docs/usage.md, «Интерфейс, роли») */}
          <p className="login__demo">
            Демо-учётки: <code>inspector</code>, <code>supervisor</code>, <code>admin</code>, <code>ml</code> — пароль
            совпадает с логином.
          </p>
        </div>
        <Stamp
          dense
          columns="1fr 1fr 1fr"
          cells={[
            { label: 'Стадии', value: 'ПД, РД, ИД' },
            { label: 'Матрица', value: '132 параметра' },
            { label: 'Версия', value: '1.0', mono: true },
          ]}
        />
      </form>
    </div>
  )
}
