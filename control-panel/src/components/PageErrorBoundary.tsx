import { Component, type ReactNode } from 'react'
import Card from './Card'

interface Props {
  children: ReactNode
}

interface State {
  hasError: boolean
  error: Error | null
}

/**
 * 页面级错误边界 — 捕获单个页面组件的渲染错误，
 * 不会影响侧边栏和导航的正常显示。
 */
export default class PageErrorBoundary extends Component<Props, State> {
  constructor(props: Props) {
    super(props)
    this.state = { hasError: false, error: null }
  }

  static getDerivedStateFromError(error: Error): State {
    return { hasError: true, error }
  }

  componentDidCatch(error: Error, info: { componentStack?: string | null }) {
    console.error('[PageErrorBoundary] Component error:', error, info.componentStack)
  }

  render() {
    if (this.state.hasError) {
      return (
        <div className="space-y-6">
          <Card>
            <div className="text-center py-12">
              <h3 style={{ fontSize: '18px', fontWeight: 600, marginBottom: '8px' }}>
                页面加载失败
              </h3>
              <p style={{ color: 'var(--color-text-tertiary)', marginBottom: '16px', fontSize: '14px' }}>
                {this.state.error?.message || '发生了未知错误'}
              </p>
              <button
                onClick={() => this.setState({ hasError: false, error: null })}
                style={{
                  padding: '8px 20px',
                  borderRadius: '8px',
                  border: 'none',
                  cursor: 'pointer',
                  fontWeight: 500,
                  fontSize: '14px',
                  backgroundColor: 'var(--color-primary)',
                  color: 'white',
                }}
              >
                重试
              </button>
            </div>
          </Card>
        </div>
      )
    }

    return this.props.children
  }
}
