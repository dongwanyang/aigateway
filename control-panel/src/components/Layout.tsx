import { useEffect, useState } from 'react'
import {
  Activity,
  BookOpen,
  Bot,
  Database,
  DollarSign,
  FileText,
  LayoutDashboard,
  LogOut,
  Menu,
  MessageSquare,
  Moon,
  Puzzle,
  Settings,
  Shield,
  Sparkles,
  Sun,
  X,
} from 'lucide-react'
import { NavLink, useLocation } from 'react-router'
import { useTheme } from '@/hooks/useTheme'
import { useAuth } from '@/contexts/AuthContext'
import CapabilityBanner from '@/components/CapabilityBanner'

const navGroups = [
  {
    label: '工作台',
    items: [
      { path: '/', label: '概览', description: '运行状态与关键指标', icon: LayoutDashboard },
      { path: '/chat', label: '聊天', description: '验证模型与路由效果', icon: MessageSquare },
    ],
  },
  {
    label: 'AI 能力',
    items: [
      { path: '/models', label: '模型配置', description: '供应商与模型路由', icon: Bot },
      { path: '/plugins', label: '插件管理', description: '请求处理流水线', icon: Puzzle },
      { path: '/knowledge', label: '知识库', description: 'RAG 与代码检索', icon: BookOpen },
    ],
  },
  {
    label: '运营治理',
    items: [
      { path: '/costs', label: '成本分析', description: '费用与 Token 用量', icon: DollarSign },
      { path: '/quotas', label: '配额管理', description: '密钥、分组与限额', icon: Shield },
      { path: '/cache', label: '缓存监控', description: '命中率与存储状态', icon: Database },
      { path: '/logs', label: '请求日志', description: '链路与异常排查', icon: FileText },
    ],
  },
  {
    label: '系统',
    items: [
      { path: '/config', label: '系统配置', description: '运行参数与调试开关', icon: Settings },
    ],
  },
]

const allNavItems = navGroups.flatMap(group => group.items)

export default function Layout({ children }: { children: React.ReactNode }) {
  const location = useLocation()
  const { toggleTheme, isDark } = useTheme()
  const { state, logout } = useAuth()
  const [mobileMenuOpen, setMobileMenuOpen] = useState(false)

  const currentItem = allNavItems.find(item => item.path === location.pathname) ?? allNavItems[0]
  const accountLabel = state.keyPrefix || '管理员'

  useEffect(() => {
    setMobileMenuOpen(false)
  }, [location.pathname])

  useEffect(() => {
    if (!mobileMenuOpen) return
    const previousOverflow = document.body.style.overflow
    document.body.style.overflow = 'hidden'
    return () => {
      document.body.style.overflow = previousOverflow
    }
  }, [mobileMenuOpen])

  async function handleLogout() {
    await logout()
  }

  const navigation = (
    <>
      <div className="sidebar-brand">
        <div className="brand-mark" aria-hidden="true">
          <Sparkles size={19} strokeWidth={2.2} />
        </div>
        <div className="min-w-0">
          <div className="brand-name">AI Gateway</div>
          <div className="brand-subtitle">Control Plane</div>
        </div>
      </div>

      <nav className="sidebar-nav" aria-label="控制台导航">
        {navGroups.map(group => (
          <div className="nav-group" key={group.label}>
            <div className="nav-group-label">{group.label}</div>
            <div className="nav-group-items">
              {group.items.map(item => {
                const Icon = item.icon
                return (
                  <NavLink
                    key={item.path}
                    to={item.path}
                    end={item.path === '/'}
                    className={({ isActive }) => `nav-item ${isActive ? 'nav-item-active' : ''}`}
                  >
                    <span className="nav-item-icon"><Icon size={18} /></span>
                    <span className="nav-item-copy">
                      <span className="nav-item-label">{item.label}</span>
                      <span className="nav-item-description">{item.description}</span>
                    </span>
                  </NavLink>
                )
              })}
            </div>
          </div>
        ))}
      </nav>

      <div className="sidebar-footer">
        <div className="sidebar-status">
          <span className="status-dot" />
          <div>
            <div className="sidebar-status-title">控制台会话已保护</div>
            <div className="sidebar-status-copy">Cookie Session · HttpOnly</div>
          </div>
        </div>
      </div>
    </>
  )

  return (
    <div className="app-shell">
      <aside className="app-sidebar desktop-sidebar">
        {navigation}
      </aside>

      {mobileMenuOpen && (
        <div className="mobile-nav-layer">
          <button
            type="button"
            className="mobile-nav-backdrop"
            aria-label="关闭导航"
            onClick={() => setMobileMenuOpen(false)}
          />
          <aside className="app-sidebar mobile-sidebar">
            <button
              type="button"
              className="icon-button mobile-close-button"
              aria-label="关闭导航"
              onClick={() => setMobileMenuOpen(false)}
            >
              <X size={19} />
            </button>
            {navigation}
          </aside>
        </div>
      )}

      <header className="app-header">
        <div className="app-header-leading">
          <button
            type="button"
            className="icon-button mobile-menu-button"
            aria-label="打开导航"
            onClick={() => setMobileMenuOpen(true)}
          >
            <Menu size={20} />
          </button>
          <div className="page-context-icon"><currentItem.icon size={18} /></div>
          <div className="min-w-0">
            <div className="page-context-title">{currentItem.label}</div>
            <div className="page-context-description">{currentItem.description}</div>
          </div>
        </div>

        <div className="app-header-actions">
          <button
            type="button"
            onClick={toggleTheme}
            className="icon-button"
            title={isDark ? '切换到亮色主题' : '切换到暗色主题'}
            aria-label={isDark ? '切换到亮色主题' : '切换到暗色主题'}
          >
            {isDark ? <Sun size={18} /> : <Moon size={18} />}
          </button>

          {state.isAuthenticated && (
            <div className="account-menu">
              <div className="account-avatar" aria-hidden="true">
                {String(accountLabel).slice(0, 1).toUpperCase()}
              </div>
              <div className="account-copy">
                <div className="account-name">{accountLabel}</div>
                <div className="account-role">系统管理员</div>
              </div>
              <button
                type="button"
                onClick={handleLogout}
                className="icon-button account-logout"
                title="退出登录"
                aria-label="退出登录"
              >
                <LogOut size={17} />
              </button>
            </div>
          )}
        </div>
      </header>

      <main className="app-main">
        <div className="app-content">
          <CapabilityBanner />
          {children}
        </div>
      </main>

      <div className="ambient-orb ambient-orb-one" aria-hidden="true" />
      <div className="ambient-orb ambient-orb-two" aria-hidden="true" />
      <Activity className="sr-only" aria-hidden="true" />
    </div>
  )
}
