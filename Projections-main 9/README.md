# Slate — MLB + NFL Model Boards

Static, free-tier app: GitHub Actions runs the Python models on a schedule, commits fresh JSON to `data/`, and Netlify auto-deploys within seconds. No servers, no cost.

## Setup (one time, ~10 minutes)

1. **Create the repo.** Push this folder to a new GitHub repo (public keeps Actions unlimited/free). Drop your `mlb_projections.py`, `nfl_projections.py`, and a `requirements.txt` into the repo root.

2. **Add API keys as secrets.** Repo → Settings → Secrets and variables → Actions: add `SGO_API_KEY`, `OWM_API_KEY`, `ODDS_API_KEY` (whichever your scripts use). Never hardcode keys — the repo is public.

3. **Make each script emit JSON.** Add an `--out` flag (or hardcode the path) that dumps your projections in the schema below to `data/mlb.json` / `data/nfl.json`. Your models already compute everything the boards display.

4. **Connect Netlify.** app.netlify.com → Add new site → Import from GitHub → pick the repo. Build command: leave empty. Publish directory: `.` — deploy. Done: every data commit from Actions redeploys the site automatically.

5. **Test the pipeline.** Repo → Actions → "Update model data" → Run workflow. If the JSON commits and the site updates, the daily crons will take it from there.

## Data files

The boards read your models' native output directly — no schema changes needed:

- `data/mlb_slate.json` — `generated_at`, `date`, `games[]` (states, scores, projections, tiers, pitchers, umps, lineups, `prop_plays[]`), `standouts.top_hr[]`
- `data/nfl_slate.json` — `games[]` with `_lines`, win/cover/over probabilities, QB projections, tiers

Games missing model fields (postponed / no lineups) render gracefully with basics only. If NFL games later include `away_skill`/`home_skill` entries with `edge`, `model_prob`, `market`, and `book_odds`, those appear automatically as props in Top Plays.

## Free-tier math

- **Netlify free:** 100 GB bandwidth/mo, 300 build minutes/mo. Each deploy here is a static copy (~seconds), and one person's usage is a rounding error.
- **GitHub Actions:** unlimited minutes on public repos. Your model runs (a few minutes/day) are fine even on a private repo's 2,000 free minutes.
- **Data:** JSON in the repo, no database needed.

## Notes

- Boards auto-refetch every 10 minutes and whenever the tab regains focus.
- `netlify.toml` sets `no-cache` on `/data/*`, so a fresh model run shows up on next fetch — no stale CDN copies.
- Add to Home Screen on iPhone installs it as a standalone app (manifest + service worker included).
