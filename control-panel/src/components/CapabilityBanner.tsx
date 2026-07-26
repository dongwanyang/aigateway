import { Layers3 } from 'lucide-react'
import { useRuntimeCapabilities } from '@/hooks/useRuntimeCapabilities'

const LABELS = {
  rag: '知识库',
  vision: '本地视觉',
} as const

export default function CapabilityBanner() {
  const { data } = useRuntimeCapabilities()
  if (!data) return null

  const limited = (Object.keys(LABELS) as Array<keyof typeof LABELS>)
    .filter(name => !data.capabilities[name].available)
  if (limited.length === 0) return null

  return (
    <div
      className="mb-5 flex items-center justify-between gap-4 rounded-lg px-4 py-3 text-sm"
      style={{
        backgroundColor: 'rgba(245, 158, 11, 0.10)',
        border: '1px solid rgba(245, 158, 11, 0.45)',
      }}
    >
      <div className="flex items-center gap-2">
        <Layers3 size={17} style={{ color: 'var(--color-warning)', flexShrink: 0 }} />
        <span>
          当前镜像：<strong>{data.profile}</strong>。受限能力：
          {limited.map(name => LABELS[name]).join('、')}。
        </span>
      </div>
      <code
        className="hidden lg:block text-xs"
        style={{ color: 'var(--color-text-secondary)', fontFamily: 'var(--font-mono)' }}
      >
        bash scripts/quickstart.sh
      </code>
    </div>
  )
}
