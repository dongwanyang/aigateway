import { useState } from 'react'

interface MediaImageProps {
  content: string
  done: boolean
}

export default function MediaImage({ content, done }: MediaImageProps) {
  const [errored, setErrored] = useState(false)

  if (!done) {
    return (
      <div className="flex items-center gap-2 text-sm" style={{ color: 'var(--color-text-secondary)' }}>
        <span className="animate-pulse">🎨 生成图片中…</span>
      </div>
    )
  }

  if (errored) {
    return (
      <div className="text-sm" style={{ color: 'var(--color-danger)' }}>
        图片加载失败
      </div>
    )
  }

  // content 可能是 URL 或 data: base64。
  // 后端默认返回 url;fallback b64_json 是裸 base64(无前缀)。
  // 生成图最常见格式为 JPEG,优先用之;若实际为 PNG/WebP 浏览器通常能自动检测。
  const src = content.startsWith('data:') || /^https?:\/\//i.test(content)
    ? content
    : `data:image/jpeg;base64,${content}`

  return (
    <img
      src={src}
      alt="生成图片"
      onError={() => setErrored(true)}
      className="max-w-full rounded-md"
      style={{ maxHeight: '400px', border: '1px solid var(--color-border)' }}
    />
  )
}
