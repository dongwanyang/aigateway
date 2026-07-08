"""Route streaming helpers (总 2).

Moved from ``aigateway_api.streaming`` and ``aigateway_api.openai_compat``
in Task 5 of the runtime-structure refactor. The API surface now keeps
only the FastAPI ``StreamingResponse`` adapter
(``aigateway_api.streaming.create_sse_response``); the SSE generator,
cache-stream simulator, and stream metrics wrapper live here.

These modules have no dependency on FastAPI or the API surface, so core
dispatch code can import them directly.
"""
