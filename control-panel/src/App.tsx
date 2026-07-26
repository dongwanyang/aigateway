import { BrowserRouter, Routes, Route } from 'react-router'
import Layout from '@/components/Layout'
import PageErrorBoundary from '@/components/PageErrorBoundary'
import Overview from '@/pages/Overview'
import Models from '@/pages/Models'
import Plugins from '@/pages/Plugins'
import Costs from '@/pages/Costs'
import Quotas from '@/pages/Quotas'
import Cache from '@/pages/Cache'
import Logs from '@/pages/Logs'
import Knowledge from '@/pages/Knowledge'
import Config from '@/pages/Config'
import Chat from '@/pages/Chat'
import Login from '@/pages/Login'
import AuthGuard from '@/components/AuthGuard'
import { ThemeProvider } from '@/hooks/useTheme'
import { AuthProvider } from '@/contexts/AuthContext'

export default function App() {
  return (
    <ThemeProvider>
      <BrowserRouter>
        <AuthProvider>
          <Routes>
            <Route path="/login" element={<Login />} />
            <Route path="*" element={
              <AuthGuard>
                <Layout>
                  <Routes>
                    <Route path="/" element={<PageErrorBoundary><Overview /></PageErrorBoundary>} />
                    <Route path="/models" element={<PageErrorBoundary><Models /></PageErrorBoundary>} />
                    <Route path="/plugins" element={<PageErrorBoundary><Plugins /></PageErrorBoundary>} />
                    <Route path="/costs" element={<PageErrorBoundary><Costs /></PageErrorBoundary>} />
                    <Route path="/quotas" element={<PageErrorBoundary><Quotas /></PageErrorBoundary>} />
                    <Route path="/cache" element={<PageErrorBoundary><Cache /></PageErrorBoundary>} />
                    <Route path="/logs" element={<PageErrorBoundary><Logs /></PageErrorBoundary>} />
                    <Route path="/knowledge" element={<PageErrorBoundary><Knowledge /></PageErrorBoundary>} />
                    <Route path="/config" element={<PageErrorBoundary><Config /></PageErrorBoundary>} />
                    <Route path="/chat" element={<PageErrorBoundary><Chat /></PageErrorBoundary>} />
                  </Routes>
                </Layout>
              </AuthGuard>
            } />
          </Routes>
        </AuthProvider>
      </BrowserRouter>
    </ThemeProvider>
  )
}
