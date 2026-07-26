import { create } from 'zustand'

interface AuthStore {
  isAuthenticated: boolean
  keyPrefix: string | null
  forceReset: boolean
  setAuthenticated: (keyPrefix: string, forceReset?: boolean) => void
  setForceReset: (forceReset: boolean) => void
  clear: () => void
}

export const useAuthStore = create<AuthStore>(set => ({
  isAuthenticated: false,
  keyPrefix: null,
  forceReset: false,
  setAuthenticated: (keyPrefix, forceReset) => set(state => ({
    isAuthenticated: true,
    keyPrefix,
    forceReset: forceReset ?? state.forceReset,
  })),
  setForceReset: forceReset => set({ forceReset }),
  clear: () => set({
    isAuthenticated: false,
    keyPrefix: null,
    forceReset: false,
  }),
}))
