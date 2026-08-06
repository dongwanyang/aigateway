import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const getVideoStatus = vi.hoisted(() => vi.fn())

vi.mock('@/api/client', async importOriginal => ({
  ...await importOriginal<typeof import('@/api/client')>(),
  getVideoStatus,
}))

const {
  VIDEO_POLL_INTERVAL_MS,
  VIDEO_POLL_TIMEOUT_MS,
  clearAllChatPolling,
  extractVideoUrl,
  isPlayableVideoUrl,
  watchVideoUntilTerminal,
} = await import('./chatRuntime')

describe('shared video polling', () => {
  beforeEach(() => {
    getVideoStatus.mockReset()
    clearAllChatPolling()
    vi.useFakeTimers()
  })

  afterEach(() => {
    clearAllChatPolling()
    vi.useRealTimers()
  })

  it('extracts the result URL from every position the upstream may use', () => {
    expect(extractVideoUrl({ video: { url: 'https://a.test/v.mp4' } })).toBe('https://a.test/v.mp4')
    expect(extractVideoUrl({ url: 'https://b.test/v.mp4' })).toBe('https://b.test/v.mp4')
    expect(extractVideoUrl({ metadata: { url: 'https://c.test/v.mp4' } })).toBe('https://c.test/v.mp4')
    expect(extractVideoUrl({ status: 'completed' })).toBeNull()
  })

  it('rejects non data/http result URLs', () => {
    expect(isPlayableVideoUrl('https://a.test/v.mp4')).toBe(true)
    expect(isPlayableVideoUrl('data:video/mp4;base64,AAAA')).toBe(true)
    expect(isPlayableVideoUrl('javascript:alert(1)')).toBe(false)
  })

  it('runs a single loop for concurrent subscribers and gives both the result', async () => {
    getVideoStatus.mockResolvedValue({ status: 'completed', metadata: { url: 'https://cdn.test/v.mp4' } })

    const first = watchVideoUntilTerminal('vid-shared')
    const second = watchVideoUntilTerminal('vid-shared')

    await vi.advanceTimersByTimeAsync(0)
    const [a, b] = await Promise.all([first.result, second.result])

    expect(a).toEqual({ kind: 'terminal', status: { status: 'completed', metadata: { url: 'https://cdn.test/v.mp4' } } })
    expect(b).toEqual(a)
    expect(getVideoStatus).toHaveBeenCalledTimes(1)

    first.release()
    second.release()
  })

  it('polls immediately instead of waiting out a full interval first', async () => {
    getVideoStatus.mockResolvedValue({ status: 'completed', url: 'https://cdn.test/v.mp4' })
    const watch = watchVideoUntilTerminal('vid-immediate')
    await vi.advanceTimersByTimeAsync(0)
    expect(getVideoStatus).toHaveBeenCalledTimes(1)
    await expect(watch.result).resolves.toMatchObject({ kind: 'terminal' })
    watch.release()
  })

  it('keeps polling past two minutes and only times out at the shared budget', async () => {
    getVideoStatus.mockResolvedValue({ status: 'in_progress' })
    const watch = watchVideoUntilTerminal('vid-slow')

    await vi.advanceTimersByTimeAsync(130_000)
    const calls = getVideoStatus.mock.calls.length
    expect(calls).toBeGreaterThan(1)

    await vi.advanceTimersByTimeAsync(VIDEO_POLL_TIMEOUT_MS)
    await expect(watch.result).resolves.toEqual({ kind: 'timeout' })
    watch.release()
  })

  it('stops the loop when the last subscriber releases', async () => {
    getVideoStatus.mockResolvedValue({ status: 'in_progress' })
    const watch = watchVideoUntilTerminal('vid-release')
    await vi.advanceTimersByTimeAsync(0)
    const before = getVideoStatus.mock.calls.length

    watch.release()
    await expect(watch.result).resolves.toEqual({ kind: 'cancelled' })

    await vi.advanceTimersByTimeAsync(VIDEO_POLL_INTERVAL_MS * 5)
    expect(getVideoStatus).toHaveBeenCalledTimes(before)
  })

  it('aborts every running loop on clearAllChatPolling', async () => {
    getVideoStatus.mockResolvedValue({ status: 'in_progress' })
    const watch = watchVideoUntilTerminal('vid-cleared')
    await vi.advanceTimersByTimeAsync(0)
    const before = getVideoStatus.mock.calls.length

    clearAllChatPolling()
    await expect(watch.result).resolves.toEqual({ kind: 'cancelled' })

    await vi.advanceTimersByTimeAsync(VIDEO_POLL_INTERVAL_MS * 5)
    expect(getVideoStatus).toHaveBeenCalledTimes(before)
  })
})
