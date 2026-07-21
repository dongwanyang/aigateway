interface RoutingBadgeProps {
  intent?: 'understanding' | 'generation:image' | 'generation:video' | null
  model?: string
}

const MAP = {
  understanding: { icon: '🧠', label: '理解' },
  'generation:image': { icon: '🎨', label: '图片' },
  'generation:video': { icon: '🎬', label: '视频' },
} as const

export default function RoutingBadge({ intent, model }: RoutingBadgeProps) {
  if (!intent) return null
  const info = MAP[intent]
  if (!info) return null
  return (
    <div className="flex items-center gap-1 mb-1 text-xs" style={{ color: 'var(--color-text-secondary)' }}>
      <span>{info.icon}</span>
      <span>{info.label}</span>
      {model && <span style={{ opacity: 0.7 }}>· {model}</span>}
    </div>
  )
}
