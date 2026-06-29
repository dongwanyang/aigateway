/**
 * 表格列拖拽调整宽度 — 全局自动生效
 * 原理：在每个 th 右侧绘制 8px 热区，mousedown 后跟踪 mousemove 调整宽度
 */

let isResizing = false
let currentTh: HTMLElement | null = null
let startX = 0
let startWidth = 0

function onMouseMove(e: MouseEvent) {
  if (!isResizing || !currentTh) return
  const diff = e.clientX - startX
  const newWidth = Math.max(40, startWidth + diff)
  currentTh.style.width = `${newWidth}px`
  currentTh.style.minWidth = `${newWidth}px`
}

function onMouseUp() {
  isResizing = false
  currentTh = null
  document.removeEventListener('mousemove', onMouseMove)
  document.removeEventListener('mouseup', onMouseUp)
  document.body.style.cursor = ''
  document.body.style.userSelect = ''
}

export function initResizableTables() {
  document.addEventListener('mousedown', (e) => {
    const target = e.target as HTMLElement
    const th = target.closest('th')
    if (!th) return

    const rect = th.getBoundingClientRect()
    // 点击位置在 th 右侧 10px 范围内 → 触发列宽调整
    if (e.clientX >= rect.right - 10) {
      e.preventDefault()
      isResizing = true
      currentTh = th
      startX = e.clientX
      startWidth = rect.width
      document.body.style.cursor = 'col-resize'
      document.body.style.userSelect = 'none'
      document.addEventListener('mousemove', onMouseMove)
      document.addEventListener('mouseup', onMouseUp)
    }
  })
}
