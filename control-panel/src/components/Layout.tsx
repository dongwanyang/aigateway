import { LayoutDashboard, Puzzle, DollarSign, Shield, Database, FileText, Sun, Moon } from 'lucide-react'
import { Link, useLocation } from 'react-router-dom'
import { useTheme } from '@/hooks/useTheme'

const navItems = [
  { path: '/', label: '概览', icon: LayoutDashboard },
  { path: '/plugins', label: '插件管理', icon: Puzzle },
  { path: '/costs', label: '成本分析', icon: DollarSign },
  { path: '/quotas', label: '配额管理', icon: Shield },
  { path: '/cache', label: '缓存监控', icon: Database },
  { path: '/logs', label: '请求日志', icon: FileText },
]

export default function Layout({ children }: { children: React.ReactNode }) {
  const location = useLocation()
  const { toggleTheme, isDark } = useTheme()

  return (
    <div className="min-h-screen" style={{ backgroundColor: 'var(--color-bg-base)', color: 'var(--color-text-primary)' }}>
      {/* 顶部导航栏 */}
      <header
        className="fixed top-0 left-0 right-0 z-50 flex items-center justify-between px-6"
        style={{ height: 'var(--nav-height)', backgroundColor: 'var(--color-bg-elevated)', borderBottom: '1px solid var(--color-border)' }}
      >
        <h1 className="text-lg font-semibold">AI Gateway Control Panel</h1>
        <button
          onClick={toggleTheme}
          className="flex items-center gap-2 px-3 py-1.5 rounded-md cursor-pointer text-sm transition-colors"
          style={{ color: 'var(--color-text-secondary)', backgroundColor: 'var(--color-bg-overlay)' }}
          title={isDark ? '切换到亮色主题' : '切换到暗色主题'}
        >
          {isDark ? <Sun size={16} /> : <Moon size={16} />}
          <span>{isDark ? '亮色' : '暗色'}</span>
        </button>
      </header>

      {/* 侧边栏 */}
      <aside
        className="fixed top-[56px] left-0 bottom-0 z-40 flex flex-col"
        style={{ width: 'var(--sidebar-width)', backgroundColor: 'var(--color-bg-elevated)', borderRight: '1px solid var(--color-border)' }}
      >
        <nav className="flex-1 py-4">
          {navItems.map(item => {
            const Icon = item.icon
            const isActive = location.pathname === item.path
            return (
              <Link
                key={item.path}
                to={item.path}
                className="flex items-center gap-3 px-4 py-2.5 text-sm transition-colors cursor-pointer"
                style={{
                  color: isActive ? 'var(--color-text-inverse)' : 'var(--color-text-secondary)',
                  backgroundColor: isActive ? 'var(--color-primary)' : 'transparent',
                }}
              >
                <Icon size={18} />
                {item.label}
              </Link>
            )
          })}
        </nav>
      </aside>

      {/* 主内容区 */}
      <main
        className="pt-[56px]"
        style={{ marginLeft: 'var(--sidebar-width)', padding: '24px' }}
      >
        <div style={{ maxWidth: '1440px' }}>
          {children}
        </div>
      </main>
    </div>
  )
}
