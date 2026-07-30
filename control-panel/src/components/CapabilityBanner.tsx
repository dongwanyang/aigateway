import { AlertTriangle, Layers3 } from 'lucide-react'
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
      className="mb-6 flex flex-col gap-3 rounded-2xl border px-4 py-3.5 text-sm sm:flex-row sm:items-center sm:justify-between"
      style={{
        background: 'linear-gradient(135deg, rgba(245, 158, 11, 0.10), rgba(245, 158, 11, 0.035))',
        borderColor: 'rgba(245, 158, 11, 0.24)',
        boxShadow: 'var(--shadow-xs)',
      }}
    >
      <div className="flex min-w-0 items-start gap-3">
        <div
          className="grid h-9 w-9 shrink-0 place-items-center rounded-xl"
          style={{ color: 'var(--color-warning)', background: 'rgba(245, 158, 11, 0.11)' }}
        >
          <AlertTriangle size={17} />
        </div>
        <div className="min-w-0">
          <div className="flex items-center gap-2 text-xs font-semibold">
            <Layers3 size={14} style={{ color: 'var(--color-warning)' }} />
            当前镜像能力受限
          </div>
          <p className="mt-1 text-xs leading-5" style={{ color: 'var(--color-text-tertiary)' }}>
            当前运行 <strong style={{ color: 'var(--color-text-secondary)' }}>{data.profile}</strong> profile，
            暂不可用：{limited.map(name => LABELS[name]).join('、')}。
          </p>
        </div>
      </div>
      <code
        className="w-fit rounded-lg px-2.5 py-1.5 text-[10px]"
        style={{
          color: 'var(--color-text-secondary)',
          background: 'var(--color-bg-overlay)',
          border: '1px solid var(--color-border)',
          fontFamily: 'var(--font-mono)',
        }}
      >
        bash scripts/quickstart.sh
      </code>
    </div>
  )
}
