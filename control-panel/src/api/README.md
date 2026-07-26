# Control-panel API module boundaries

The control-panel authentication boundary is deliberately split:

- `authSession.ts` is the only browser-console authentication client. It sends
  `username` and `password` to `/auth/session`, receives no secret in the body,
  and relies on the HttpOnly `aigateway_session` cookie for subsequent browser
  requests.
- `client.ts` is the general resource/API client used after a browser session is
  already established. It must not be used to create console login sessions or
  to exchange API keys for browser cookies.

API keys are machine credentials only. They may be used by SDKs, CLIs, or server
integrations through `Authorization: Bearer ...` or `x-api-key` headers, but they
are not valid control-panel login credentials.

Do not add new `api_key`, `saveApiKey`, or API-key-login helpers to the browser
console path. Keep console login code in `authSession.ts` and use the shape:

```ts
{ username: string, password: string }
```
