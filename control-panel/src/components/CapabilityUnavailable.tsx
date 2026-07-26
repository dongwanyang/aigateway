import { AlertTriangle, Terminal } from 'lucide-react'
import type { RuntimeCapability } from '@/api/client'

interface CapabilityUnavailableProps {
  title: string
  description: string
  capability: RuntimeCapability
}

export default function CapabilityUnavailable({
  title,
  description,
  capability,
}: CapabilityUnavailableProps) {
  return (
    <div
      className="rounded-xl p-6"
      style={{
        backgroundColor: 'var(--color-bg-elevated)',
        border: '1px solid var(--color-warning)',
      }}
    >
      <div className="flex items-start gap-3">
        <AlertTriangle size={22} style={{ color: 'var(--color-warning)', flexShrink: 0 }} />
        <div>
          <h2 className="text-lg font-semibold">{title}</h2>
          <p className="mt-2 text-sm" style={{ color: 'var(--color-text-secondary)' }}>
            {description}
          </p>
          {capability.reason && (
            <p className="mt-2 text-sm" style={{ color: 'var(--color-warning)' }}>
              当前状态：{capability.reason}
            </p>
          )}
          {capability.install_command && (
            <div
              className="mt-4 flex items-center gap-2 rounded-lg px-3 py-2"
              style={{ backgroundColor: 'var(--color-bg-overlay)' }}
            >
              <Terminal size={15} style={{ color: 'var(--color-text-tertiary)' }} />
              <code className="text-sm" style={{ fontFamily: 'var(--font-mono)' }}>
                {capability.install_command}
              </code>
            </div>
          )}
          <p className="mt-3 text-xs" style={{ color: 'var(--color-text-tertiary)' }}>
            安装向导可重复运行，升级能力不会删除已有数据卷。
          </p>
        </div>
      </div>
    </div>
  )
}
