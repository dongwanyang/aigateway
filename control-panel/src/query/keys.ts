export const queryKeys = {
  auth: {
    session: ['auth', 'session'] as const,
  },
  runtime: {
    capabilities: ['runtime', 'capabilities'] as const,
  },
  overview: {
    health: ['overview', 'health'] as const,
    metrics: ['overview', 'metrics'] as const,
  },
  config: {
    full: ['config', 'full'] as const,
  },
  logs: {
    all: ['logs'] as const,
    list: (params: { page: number; pageSize: number; status: string; cacheOnly: boolean }) =>
      ['logs', 'list', params] as const,
    trace: (traceId: string) => ['logs', 'trace', traceId] as const,
  },
} as const
