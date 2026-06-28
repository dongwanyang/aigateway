import { BrowserRouter, Routes, Route } from 'react-router-dom'
import Layout from '@/components/Layout'
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
            <Route path="/" element={<Overview />} />
            <Route path="/plugins" element={<Plugins />} />
            <Route path="/costs" element={<Costs />} />
            <Route path="/quotas" element={<Quotas />} />
            <Route path="/cache" element={<Cache />} />
            <Route path="/logs" element={<Logs />} />
          </Routes>
        </Layout>
      </BrowserRouter>
    </ThemeProvider>
  )
}
