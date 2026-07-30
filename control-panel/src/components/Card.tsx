import { CSSProperties, ReactNode } from 'react'

export default function Card({ children, title, className = '', style }: {
  children: ReactNode
  title?: string
  className?: string
  style?: CSSProperties
}) {
  return (
    <section
      className={`card ${className}`}
      style={{ padding: 'clamp(18px, 2vw, 24px)', ...style }}
    >
      {title && <h3 className="card-title">{title}</h3>}
      {children}
    </section>
  )
}
