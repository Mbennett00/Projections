/**
 * projex-refresh -- Cloudflare Worker that lets the "Run now" button start the
 * GitHub Action.
 *
 * The button cannot call GitHub directly: workflow_dispatch needs a token with
 * write access, and anything in a static site is public. This Worker holds the
 * token as a secret and forwards the request.
 *
 * Setup
 *   1. wrangler deploy   (or paste this into the Cloudflare dashboard editor)
 *   2. Add a secret named GITHUB_TOKEN -- a fine-grained PAT scoped to this one
 *      repo with Actions: read & write. Never put it in this file.
 *   3. Put the deployed URL into RUN_TRIGGER_URL in picks.js.
 *
 * The CORS headers matter. Without them the browser blocks the response and
 * the button appears to do nothing at all.
 */

const OWNER = 'Mbennett00';
const REPO = 'Projections';
const WORKFLOW = 'update-data.yml';

// Only these origins may trigger a run. '*' would let any site burn your
// Actions minutes and API quota.
const ALLOWED_ORIGINS = [
  'https://projections-91c.pages.dev',
];

function corsHeaders(origin) {
  const allowed = ALLOWED_ORIGINS.includes(origin) ? origin : ALLOWED_ORIGINS[0];
  return {
    'Access-Control-Allow-Origin': allowed,
    'Access-Control-Allow-Methods': 'POST, OPTIONS',
    'Access-Control-Allow-Headers': 'Content-Type',
    'Access-Control-Max-Age': '86400',
  };
}

export default {
  async fetch(request, env) {
    const origin = request.headers.get('Origin') || '';
    const cors = corsHeaders(origin);

    // The browser sends this before the POST. Answering it is what makes the
    // real request possible.
    if (request.method === 'OPTIONS') {
      return new Response(null, { status: 204, headers: cors });
    }

    if (request.method !== 'POST') {
      return new Response('POST only', { status: 405, headers: cors });
    }

    if (!env.GITHUB_TOKEN) {
      return new Response(JSON.stringify({ error: 'GITHUB_TOKEN secret not set' }),
        { status: 500, headers: { ...cors, 'Content-Type': 'application/json' } });
    }

    const url = `https://api.github.com/repos/${OWNER}/${REPO}/actions/workflows/${WORKFLOW}/dispatches`;
    const gh = await fetch(url, {
      method: 'POST',
      headers: {
        'Authorization': `Bearer ${env.GITHUB_TOKEN}`,
        'Accept': 'application/vnd.github+json',
        'X-GitHub-Api-Version': '2022-11-28',
        // GitHub rejects requests without a User-Agent.
        'User-Agent': 'projex-refresh-worker',
        'Content-Type': 'application/json',
      },
      body: JSON.stringify({ ref: 'main' }),
    });

    // A successful dispatch returns 204 with no body.
    if (gh.status === 204) {
      return new Response(JSON.stringify({ ok: true }),
        { status: 200, headers: { ...cors, 'Content-Type': 'application/json' } });
    }

    const detail = await gh.text();
    return new Response(JSON.stringify({ ok: false, status: gh.status, detail }),
      { status: 502, headers: { ...cors, 'Content-Type': 'application/json' } });
  },
};
