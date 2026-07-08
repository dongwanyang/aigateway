"""Route metrics closure (总 2).

Hosts cost-estimation helpers that previously lived in the API surface
(``aigateway_api.openai_compat``). Moved here so core dispatch code can
import them without reaching into the API layer.
"""
