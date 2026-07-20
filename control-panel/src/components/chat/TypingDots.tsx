/**
 * 等待大模型首个 token 时的"正在输入"三点动画。
 *
 * 触发条件:助手消息已创建(streaming 中)但 content 还为空 —— 即请求已发出、
 * 后端还在分类/路由/首 token 生成阶段。一旦 content 开始流入,MessageBubble
 * 会切到流式光标 ▌,本组件不再渲染。
 *
 * 三个圆点用 CSS animation 错峰跳动(0/0.2s/0.4s 延迟),无需 JS 计时器。
 */
export default function TypingDots() {
  return (
    <div className="flex items-center gap-1 py-1" aria-label="正在思考">
      {[0, 1, 2].map(i => (
        <span
          key={i}
          className="inline-block rounded-full"
          style={{
            width: 6,
            height: 6,
            backgroundColor: 'var(--color-text-secondary)',
            opacity: 0.4,
            animation: `typing-bounce 1.2s ${i * 0.2}s infinite ease-in-out`,
          }}
        />
      ))}
      <style>{`
        @keyframes typing-bounce {
          0%, 60%, 100% { transform: translateY(0); opacity: 0.4; }
          30%           { transform: translateY(-4px); opacity: 1; }
        }
      `}</style>
    </div>
  )
}
