import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { BrowserRouter } from 'react-router-dom'
import '@fontsource-variable/golos-text/index.css'
import '@fontsource/pt-mono/400.css'
import './styles/tokens.css'
import './styles/base.css'
import './styles/ui.css'
import './styles/pages.css'
import './styles/workbench.css'
import { App } from './App'
import { AuthProvider } from './auth/AuthContext'
import { ToastProvider } from './components/Toasts'

const root = document.getElementById('root')
if (!root) throw new Error('Нет элемента #root')

createRoot(root).render(
  <StrictMode>
    <BrowserRouter>
      <ToastProvider>
        <AuthProvider>
          <App />
        </AuthProvider>
      </ToastProvider>
    </BrowserRouter>
  </StrictMode>,
)
