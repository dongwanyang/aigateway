import { useCallback, useEffect, useRef, useState } from 'react'
import { X, ZoomIn, ZoomOut, RotateCcw } from 'lucide-react'

interface ImageLightboxProps {
  /** data URL 或任意图片源 */
  src: string
  alt?: string
  /** 触发器:渲染为缩略图(点击打开) */
  thumbStyle?: React.CSSProperties
  thumbAlt?: string
}

/**
 * 图片放大查看(lightbox)。点击缩略图打开全屏浮层,支持滚轮缩放 + 拖拽平移,
 * 用于查看 4K 高清图的细节(聊天窗气泡里图片被 CSS 缩到几百 px,看不出 4K 差异)。
 *
 * 注意:滚轮缩放用原生 addEventListener({ passive: false }) 而非 React 的 onWheel,
 * 因为 React 在 Chrome 里把 wheel 注册成 passive listener,preventDefault() 会被忽略
 * 并抛 "Unable to preventDefault inside passive event listener"。
 */
export default function ImageLightbox({ src, alt = '', thumbStyle, thumbAlt }: ImageLightboxProps) {
  const [open, setOpen] = useState(false)
  const [scale, setScale] = useState(1)
  const [tx, setTx] = useState(0)
  const [ty, setTy] = useState(0)
  const dragRef = useRef<{ x: number; y: number; tx: number; ty: number } | null>(null)
  const overlayImgRef = useRef<HTMLImageElement | null>(null)

  const reset = useCallback(() => {
    setScale(1)
    setTx(0)
    setTy(0)
  }, [])

  // Esc 关闭
  useEffect(() => {
    if (!open) return
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') setOpen(false)
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [open])

  // 打开时锁定 body 滚动
  useEffect(() => {
    if (!open) return
    const prev = document.body.style.overflow
    document.body.style.overflow = 'hidden'
    return () => { document.body.style.overflow = prev }
  }, [open])

  // 打开时把焦点移入浮层(方便键盘用户立即操作;Esc/Tab 在 dialog 内循环)
  useEffect(() => {
    if (!open) return
    const el = overlayImgRef.current
    el?.focus()
  }, [open])

  // 打开浮层时重置缩放/位移
  useEffect(() => {
    if (open) reset()
  }, [open, reset])

  // 原生非 passive wheel 监听(打开时挂到浮层图片上),preventDefault 阻止页面滚动
  useEffect(() => {
    const el = overlayImgRef.current
    if (!open || !el) return
    const onWheel = (e: WheelEvent) => {
      e.preventDefault()
      const delta = e.deltaY > 0 ? 0.85 : 1.18
      setScale(s => Math.min(8, Math.max(0.2, s * delta)))
    }
    el.addEventListener('wheel', onWheel, { passive: false })
    return () => el.removeEventListener('wheel', onWheel)
  }, [open])

  const onPointerDown = (e: React.PointerEvent) => {
    if (scale <= 1) return
    (e.target as HTMLElement).setPointerCapture(e.pointerId)
    dragRef.current = { x: e.clientX, y: e.clientY, tx, ty }
  }
  const onPointerMove = (e: React.PointerEvent) => {
    if (!dragRef.current) return
    setTx(dragRef.current.tx + (e.clientX - dragRef.current.x))
    setTy(dragRef.current.ty + (e.clientY - dragRef.current.y))
  }
  const onPointerUp = (e: React.PointerEvent) => {
    dragRef.current = null
    try { (e.target as HTMLElement).releasePointerCapture(e.pointerId) } catch { /* ignore */ }
  }

  return (
    <>
      <img
        src={src}
        alt={thumbAlt ?? alt}
        onClick={() => setOpen(true)}
        className="cursor-zoom-in"
        style={{ maxWidth: '100%', borderRadius: 6, border: '1px solid var(--color-border)', ...thumbStyle }}
      />
      {open && (
        <div
          className="fixed inset-0 z-[100] flex items-center justify-center"
          style={{ backgroundColor: 'rgba(0,0,0,0.9)' }}
          role="dialog"
          aria-modal="true"
          aria-label={alt || '图片预览'}
          onClick={() => setOpen(false)}
        >
          {/* 顶部工具栏 */}
          <div className="absolute top-0 left-0 right-0 flex items-center justify-between px-4 py-2" style={{ color: '#fff' }} onClick={e => e.stopPropagation()}>
            <span className="text-sm" style={{ opacity: 0.7 }}>{Math.round(scale * 100)}%</span>
            <div className="flex items-center gap-2">
              <button onClick={() => setScale(s => Math.min(8, s * 1.3))} className="cursor-pointer p-1.5 rounded" style={{ background: 'rgba(255,255,255,0.1)' }} title="放大"><ZoomIn size={18} /></button>
              <button onClick={() => setScale(s => Math.max(0.2, s / 1.3))} className="cursor-pointer p-1.5 rounded" style={{ background: 'rgba(255,255,255,0.1)' }} title="缩小"><ZoomOut size={18} /></button>
              <button onClick={reset} className="cursor-pointer p-1.5 rounded" style={{ background: 'rgba(255,255,255,0.1)' }} title="重置"><RotateCcw size={18} /></button>
              <button onClick={() => setOpen(false)} className="cursor-pointer p-1.5 rounded" style={{ background: 'rgba(255,255,255,0.1)' }} title="关闭"><X size={18} /></button>
            </div>
          </div>
          {/* 图片:点击背景关闭,但点图片本身不关(避免拖拽误关) */}
          <img
            ref={overlayImgRef}
            src={src}
            alt={alt}
            tabIndex={-1}
            onPointerDown={onPointerDown}
            onPointerMove={onPointerMove}
            onPointerUp={onPointerUp}
            onClick={e => e.stopPropagation()}
            draggable={false}
            style={{
              maxWidth: '90vw',
              maxHeight: '90vh',
              transform: `translate(${tx}px, ${ty}px) scale(${scale})`,
              transition: dragRef.current ? 'none' : 'transform 0.1s',
              cursor: scale > 1 ? (dragRef.current ? 'grabbing' : 'grab') : 'default',
              touchAction: 'none',
            }}
          />
        </div>
      )}
    </>
  )
}
