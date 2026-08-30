# Deploying the dashboard

The public demo URL serves the **read-only journal viewer only**. The trading
agent is never deployed: it needs live Alpaca credentials and must run on a
machine you control.

## What is in the image

`Dockerfile` copies exactly four things — `agent/__init__.py`,
`agent/journal.py`, `dashboard/`, and `journal/trades.db`. It installs only
FastAPI and uvicorn.

Verified on the built image:

```
/app/agent/__init__.py
/app/agent/journal.py
/app/dashboard/api.py
/app/dashboard/static/index.html
/app/journal/trades.db
```

- **No `.env`** — excluded by `.dockerignore` and never copied.
- **No `alpaca-py`, no `anthropic`, no MCP client** — `import alpaca` fails
  inside the container. The image physically cannot place an order.
- **No secret environment variables.**
- Runs as an unprivileged user (`viewer`, uid 1000).

`dashboard/api.py` has no route that places, cancels, or modifies an order.

## Run it locally

```bash
docker build -t vetoed-dashboard .
docker run -p 7860:7860 vetoed-dashboard
# http://localhost:7860
```

## Option A — Render (free, one click)

`render.yaml` is committed, so Render picks the settings up automatically.

1. Sign in at <https://render.com> with GitHub.
2. **New → Blueprint**, choose the `Vetoed` repository.
3. Render reads `render.yaml`, builds the Dockerfile, and gives you
   `https://vetoed-dashboard.onrender.com`.

The free plan sleeps after 15 minutes idle, so a cold start takes about 30
seconds. Open the link a minute before demoing it.

## Option B — Hugging Face Spaces (free, always warm)

No card, no sleep, and it serves port 7860 by default, which the Dockerfile
already uses.

1. Create a Space at <https://huggingface.co/new-space>, SDK **Docker**, blank
   template.
2. Push this repository to the Space remote:

   ```bash
   git remote add space https://huggingface.co/spaces/<user>/<space-name>
   git push space main
   ```

3. The Space needs a `README.md` with Spaces front matter at the top:

   ```
   ---
   title: Vetoed
   emoji: 🛡️
   colorFrom: gray
   colorTo: green
   sdk: docker
   app_port: 7860
   ---
   ```

   Add it on the Space only — it is not needed in the GitHub repo.

## Keeping the demo current

The dashboard reads `journal/trades.db`, which is committed. The agent writes
to your local copy, so after a trading session:

```bash
git add journal/trades.db
git commit -m "Update journal"
git push
```

Render and Spaces both redeploy on push, so the demo updates itself.

**The journal is safe to commit.** It was audited before being un-ignored: no
API keys, no Alpaca account id, no order ids, paper trading only. Re-check
after live trading, because `orders.raw_json` will then contain real Alpaca
order responses:

```bash
python - <<'PY'
import sqlite3, re
c = sqlite3.connect("journal/trades.db")
bad = re.compile(r"PK[A-Z0-9]{12,}|sk-ant|secret|api[-_]?key", re.I)
for t in ("runs","decisions","orders","equity_snapshots"):
    for r in c.execute("SELECT * FROM %s" % t):
        for v in r:
            if v and bad.search(str(v)):
                print("CHECK", t, str(v)[:120])
print("scan complete")
PY
```
