# Deploying the SDP Investor Finder

## Run locally (first sanity check)

```bash
cd ~/sdp-network
source .venv/bin/activate              # or use .venv/bin/streamlit directly
.venv/bin/streamlit run app.py
```

Opens at <http://localhost:8501>. If `.env` has `ANTHROPIC_API_KEY`, deal
extraction works immediately. To set the gate password locally:

```bash
echo 'APP_PASSWORD=pick-something-shared' >> .env
```

(With no password set the app runs un-gated — fine for localhost.)

## Deploy to Streamlit Community Cloud (free)

1. **Push to a private GitHub repo.** The repo needs `app.py`, `requirements.txt`,
   `scripts/`, `taxonomy.yml`, `db/investors.db`, `db/migrations/`. Critical:
   include `db/investors.db` so the deployed app can read it (or upload it via
   the dashboard if you don't want it in git).

2. **`requirements.txt`** is already up to date. Streamlit Cloud will pip-install it.

3. **Connect the repo** at <https://share.streamlit.io> → "New app" →
   pick the repo, branch, and `app.py`.

4. **Set secrets** (Settings → Secrets). Paste:
   ```toml
   ANTHROPIC_API_KEY = "sk-ant-api03-..."
   APP_PASSWORD = "share-this-with-the-team"
   ```

5. **Deploy.** Public URL becomes `https://<your-app>.streamlit.app`. Give
   the URL + password to the team.

### Notes
- Streamlit Cloud's filesystem is **ephemeral** — `db/investors.db` will be
  re-read from the git repo on every deploy. To update the DB after a re-ingest
  run locally, commit the new `investors.db` and push.
- Cloud free tier has limited resources; if performance suffers, downgrade
  the Anthropic model in `.env` (e.g., `CLAUDE_MODEL_HAIKU=claude-haiku-4-5-20251001`
  is already the cheapest) or move to Railway.

## Alternative: deploy on Railway ($5/mo, persistent DB)

Railway lets you keep `db/investors.db` writable across deploys.

1. `railway login && railway init` in the `sdp-network/` folder.
2. Add a `Procfile` with: `web: streamlit run app.py --server.port=$PORT --server.address=0.0.0.0`
3. Set environment variables in the Railway dashboard (same keys as Streamlit Cloud).
4. Mount a volume at `/app/db` if you want the DB to persist across deploys.

## How users will use it

1. Open the URL → enter shared password.
2. **Tab 1**: upload a PDF deck or Word doc, OR
3. **Tab 2**: paste a transcript / memo / freeform text.
4. App reads the file → calls Claude Haiku → shows the detected criteria.
5. Optionally **edit** the criteria (capital type, sector, geo, check size).
6. Click **Find investors**. Top N matches render as cards with:
   - Firm name, type, score
   - Primary contact + email + SDP relationship owner
   - Last touch summary (status, feedback)
   - Mandate notes, firm strategy
   - Score breakdown (transparent — every point is auditable)
   - Links to website and LinkedIn
7. Sidebar shows network coverage (how many firms have Attio data, etc.).
8. **Download CSV** of all surviving candidates.

## Cost per query

- One Haiku call to extract deal criteria from the deck: **~$0.005**
- Scorer runs entirely against the local SQLite DB — free.
- Streamlit Cloud hosting: free.
- Anthropic spend: at ~5 queries / day per team member × 5 members = ~$0.40/month.
