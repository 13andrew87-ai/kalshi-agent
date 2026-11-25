# Deploying Kalshi Agent

The repository is secret-free by construction: the gate password comes from the
`KALSHI_WEB_PASSWORD` environment variable (the service **fails closed** without
it), the Kalshi key ID / private key come from env vars or from
`state/credentials/` **inside the container's `/app/state` volume**, and
`.gitignore` / `.dockerignore` keep `state/`, `reports/`, `*.pem` and `.env*`
out of both git and the Docker build context.

Pick ONE of the paths below. Everything here runs the **web dashboard +
paper/sim agent** by default. **Nothing in this bundle enables real-money
trading** — that is a separate, deliberate step (see "Live (real-money) mode").

---

## 1. Docker on any machine (VPS, home server, …)

```bash
git clone <your-fork-url> && cd kalshi-agent
docker build -t kalshi-agent .
docker run -d --name kalshi \
  -p 8080:8080 \
  -e KALSHI_WEB_PASSWORD='pick-a-strong-one' \
  -v kalshi-state:/app/state \
  --restart unless-stopped \
  kalshi-agent
curl -fsS http://localhost:8080/healthz   # -> ok
```

Open `http://<host>:8080/` — sign in with the password. The sign-in redirects
to a token-prefixed URL (`/app/<token>/`); that suffix is derived from the
password and is what proxies cleanly behind any reverse proxy.

`docker compose up -d --build` does the same, reading `KALSHI_WEB_PASSWORD`
from your shell (or an untracked `.env` file next to the compose file).

## 2. Fly.io

```bash
fly auth login
fly launch --no-deploy --copy-config   # uses fly.toml; set your app name/region
fly secrets set KALSHI_WEB_PASSWORD='pick-a-strong-one'
fly volumes create kalshi_state --size 1
fly deploy
```

`auto_stop_machines = false` is deliberate: the machine keeps running the
agent. Add `fly secrets set KALSHI_API_KEY_ID=… KALSHI_PRIVATE_KEY_PATH=/app/state/credentials/kalshi_private_key.pem`
only when you mean to arm live mode.

## 3. Render.com

Push the repo, then **New → Blueprint** and select it (`render.yaml`):
set `KALSHI_WEB_PASSWORD` in the Render UI when prompted. Free instances sleep
when idle ("cold start" on first open); a paid instance + the commented-out
`disk:` block gives you persistent state (uploaded keys survive redeploys).

## 4. Named Cloudflare tunnel (no public host needed, your laptop/VPS stays the origin)

```bash
cloudflared tunnel login
cloudflared tunnel create kalshi
cloudflared tunnel route dns kalshi kalshi.yourdomain.com
KALSHI_WEB_PASSWORD='pick-a-strong-one' \
  cloudflared tunnel run --url http://localhost:8080 kalshi
```

DNS + HTTPS handled by Cloudflare; the gate still protects everything but
`/healthz`.

---

## First-run credentials (Kalshi)

The container starts empty. Two ways to provision:

1. **Via the UI** (easiest): open the app → **API Credentials** → either
   generate a fresh Kalshi keypair in-app (saved into `/app/state`) or paste an
   existing private key. Persistence requires the volume (all paths above
   mount one).
2. **Via files**: mount your PEM into the volume at
   `/app/state/credentials/kalshi_private_key.pem` and set
   `KALSHI_API_KEY_ID` + `KALSHI_PRIVATE_KEY_PATH`.

Never bake keys into the image or commit them.

## Live (real-money) mode — separate, deliberate step

The shipped image runs the paper/sim agent. To trade REAL money you must all
of:

1. set `KALSHI_LIVE_CONFIRM=YES`,
2. provide `KALSHI_API_KEY_ID` and a readable `KALSHI_PRIVATE_KEY_PATH`,
3. and override the container command, e.g.:

```bash
docker run -d --name kalshi-live \
  -e KALSHI_WEB_PASSWORD='…' -e KALSHI_API_KEY_ID='…' \
  -e KALSHI_PRIVATE_KEY_PATH=/app/state/credentials/kalshi_private_key.pem \
  -e KALSHI_LIVE_CONFIRM=YES \
  -v kalshi-state:/app/state --restart unless-stopped \
  kalshi-agent \
  python -m kalshi_agent.cli --config config-live.yaml live --live --no-dry-run \
    --cycles 60 --poll 45
```

Risk caps in `config-live.yaml` (1–2 contract clips, 15% daily-loss halt, kill
switch on 6 consecutive losses, 50% max exposure, `$0`-floor protections) apply.
Review them before arming. **Simulated performance is not a promise of profit.**

## Operations

- **Rotate the app password**: restart the container with a new
  `KALSHI_WEB_PASSWORD`. The token-prefixed URLs are derived from the password,
  so every old link/QR dies instantly.
- **Logs**: `docker logs -f kalshi`
- **Health**: `GET /healthz` → `ok` (unauthenticated, for load balancers)
- **Backup**: snapshot the `state` volume (it holds the encrypted-at-rest
  material for the account you configure); treat it like a secret.
