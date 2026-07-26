import { useQuery, useQueryClient } from '@tanstack/react-query'
import { getRuntimeCapabilities } from '@/api/client'
import { queryKeys } from '@/query/keys'
import { useAuthStore } from '@/stores/authStore'

export function useRuntimeCapabilities() {
  const queryClient = useQueryClient()
  const isAuthenticated = useAuthStore(state => state.isAuthenticated)
  const query = useQuery({
    queryKey: queryKeys.runtime.capabilities,
    queryFn: async () => (await getRuntimeCapabilities()).data,
    enabled: isAuthenticated,
    staleTime: 60_000,
  })

  return {
    data: query.data ?? null,
    loading: query.isLoading,
    error: query.error instanceof Error ? query.error.message : null,
    refresh: async () => {
      await queryClient.invalidateQueries({ queryKey: queryKeys.runtime.capabilities })
    },
  }
}
