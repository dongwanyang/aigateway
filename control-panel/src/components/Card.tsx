import { CSSProperties, ReactNode } from 'react'

export default function Card({ children, title, className = '', style }: {
  children: ReactNode
  title?: string
  className?: string
  style?: CSSProperties
}) {
  return (
    <div className={`card ${className}`} style={{ padding: '24px', ...style }}>
      {title && (
        <h3 className="mb-4 text-md font-semibold" style={{ color: 'var(--color-text-primary)' }}>
          {title}
        </h3>
      )}
      {children}
    </div>
  )
}
