# Run-now Worker

Lets the ↻ button on the hub start the `update-data.yml` Action on demand,
instead of waiting for the next cron.

## Why a Worker at all

`workflow_dispatch` requires a token with write access to the repo. This site
is static and public, so a token in the page source would be readable by
anyone. The Worker holds the token server-side and is the only thing that can
talk to GitHub.

## Setup

1. **Create the token.** GitHub → Settings → Developer settings → Fine-grained
   tokens. Scope it to `Mbennett00/Projections` only, with
   **Actions: read & write**. Nothing else.

2. **Deploy the Worker.** Either `wrangler deploy` from this folder, or paste
   `worker.js` into a new Worker in the Cloudflare dashboard.

3. **Add the secret.** Worker → Settings → Variables → *Add secret*, named
   exactly `GITHUB_TOKEN`. Use a secret, not a plaintext variable.

4. **Point the site at it.** Put the Worker URL into `RUN_TRIGGER_URL` at the
   top of `picks.js`:

   ```js
   const RUN_TRIGGER_URL = 'https://projex-refresh.your-name.workers.dev';
   ```

5. **Check `ALLOWED_ORIGINS`** in `worker.js` matches the site's domain.

## The workflow must allow manual runs

`update-data.yml` needs `workflow_dispatch:` under `on:`. It already does —
without it GitHub returns 422 and the button reports the failure.

## If the button does nothing

The hub now says why rather than failing quietly:

| message | cause |
|---|---|
| `No Run-now URL set` | `RUN_TRIGGER_URL` is still empty in `picks.js` |
| `blocked (check the Worker's CORS headers)` | Worker didn't answer the preflight, or the origin isn't in `ALLOWED_ORIGINS` |
| `HTTP 401` / `403` | token missing, expired, or lacks Actions write |
| `HTTP 404` | wrong owner/repo/workflow filename in `worker.js` |
| `HTTP 422` | workflow has no `workflow_dispatch` trigger, or `ref` isn't a real branch |

Test the Worker directly, without the browser in the way:

```bash
curl -i -X POST https://projex-refresh.your-name.workers.dev
```

A `{"ok":true}` means GitHub accepted it — check the Actions tab.
