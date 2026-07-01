import { BrowserRouter, Routes, Route } from 'react-router-dom'
import Layout from '@/components/Layout'
import PageErrorBoundary from '@/components/PageErrorBoundary'
import Overview from '@/pages/Overview'
import Plugins from '@/pages/Plugins'
import Costs from '@/pages/Costs'
import Quotas from '@/pages/Quotas'
import Cache from '@/pages/Cache'
import Logs from '@/pages/Logs'
import Knowledge from '@/pages/Knowledge'
import Config from '@/pages/Config'
import { ThemeProvider } from '@/hooks/useTheme'

export default function App() {
  return (
    <ThemeProvider>
      <BrowserRouter>
        <Layout>
          <Routes>
            <Route path="/" element={<PageErrorBoundary><Overview /></PageErrorBoundary>} />
            <Route path="/plugins" element={<PageErrorBoundary><Plugins /></PageErrorBoundary>} />
            <Route path="/costs" element={<PageErrorBoundary><Costs /></PageErrorBoundary>} />
            <Route path="/quotas" element={<PageErrorBoundary><Quotas /></PageErrorBoundary>} />
            <Route path="/cache" element={<PageErrorBoundary><Cache /></PageErrorBoundary>} />
            <Route path="/logs" element={<PageErrorBoundary><Logs /></PageErrorBoundary>} />
            <Route path="/knowledge" element={<PageErrorBoundary><Knowledge /></PageErrorBoundary>} />
            <Route path="/config" element={<PageErrorBoundary><Config /></PageErrorBoundary>} />
          </Routes>
        </Layout>
      </BrowserRouter>
    </ThemeProvider>
  )
}
