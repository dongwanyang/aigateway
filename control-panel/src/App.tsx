import { BrowserRouter, Routes, Route } from 'react-router-dom'
import Layout from '@/components/Layout'
import PageErrorBoundary from '@/components/PageErrorBoundary'
import Overview from '@/pages/Overview'
import Plugins from '@/pages/Plugins'
import Costs from '@/pages/Costs'
import Quotas from '@/pages/Quotas'
import Cache from '@/pages/Cache'
import Logs from '@/pages/Logs'
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
          </Routes>
        </Layout>
      </BrowserRouter>
    </ThemeProvider>
  )
}
