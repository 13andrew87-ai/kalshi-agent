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

## Fly.io production (kalshi-agent-andrew) — what actually runs

Single always-on machine (`min_machines_running = 1`, shared-cpu-1x, 512MB) in
`lax`, volume `kalshi_state` mounted at `/app/state` (survives restarts).

The image entrypoint (`fly-entrypoint.sh`) starts TWO processes on that one
machine — never two machines, never two agents:

1. **Live trading agent** — `python -m kalshi_agent.cli --config config-live.yaml live --live`
   under a watchdog loop (crash → restart after 5s). It ARMS ITSELF ONLY if the
   Kalshi credentials exist on the volume (`/app/state/web/credentials/`, the
   same PEM + key id saved via the account console's API-keys page; key id is
   read from `meta.json`, never from a pasted chat value). `KALSHI_LIVE_CONFIRM=YES`
   is set inside the entrypoint. Agent stdout → `/app/state/live/agent-stdout.log`.
2. **Account console (web)** — main process; unchanged YES-gated manual betting.

**This machine trades real money autonomously** under the same rules as the old
local agent: 8¢ edge bar, 8–30¢ band, 2 contracts max, hard −15¢ stop / +5¢ TP,
50% per-market, 8 positions / 100% exposure caps, AI exits via free Groq. The
prompt box on the site still requires an explicit YES tap per bet — including
each leg of a multi-bet batch.

### Deploys

Use **`flyctl deploy --strategy immediate`**. The default `rolling` strategy
puts the machine into `stopped` state and leaves the site down until manually
started; `immediate` replaces it in place and it comes back on its own (~15s,
health check flips to passing). Every deploy reboots the machine: the live
agent pauses ~15–30s mid-cycle; in-flight IOC orders are atomic at the
exchange, and state is rebuilt from the exchange next cycle — nothing else to
care about. Same image, one reboot, no duplicates.

- Health: `curl https://kalshi-agent-andrew.fly.dev/healthz` → `ok`
- Agent log: `flyctl ssh console -a kalshi-agent-andrew -C "tail -20 /app/state/live/agent-stdout.log"`
- Machine state: `flyctl machine list -a kalshi-agent-andrew`
- Fly secrets hold only `KALSHI_API_KEY_ID` (fallback); no PEM, no PATs, nothing
  else. The Groq key is read from `/app/state/groq_api_key` if present — set it
  once via ssh if AI exits are wanted (it is; they're on in config-live.yaml).

### Known operational truths

- The Kalshi **v2 order endpoints** are mandatory (`POST /trade-api/v2/portfolio/events/orders`,
  `DELETE .../orders/{id}`; payload is YES-leg single-book `bid`/`ask` with
  fixed-point dollar prices — buy NO is encoded as ask YES at `1 - price`). Legacy
  `/portfolio/orders` returns `410 deprecated_v1_order_endpoint`.
- If the volume has no credentials, the agent does NOT arm; the console still
  serves and logs "credentials missing on volume — live agent DISABLED".
- Sandbox (Arena) sessions die with every bounce; the Fly machine is the
  permanent home. Do NOT run a second agent against the same account anywhere.

## Fly machine ops (post-2026-11-26 fixes)

- Machine restart policy: **`always`** (`flyctl machine update <id> --restart always`).
  Without it, a clean SIGINT (exit 0) left the machine stopped — deploys did not
  auto-restart it (on-failure default). Now deploys/reboots self-heal.
- `auto_stop_machines = "off"`, `min_machines_running = 1` — never idle-stop.
- `--strategy immediate` for deploys; even so, verify afterwards:
  `flyctl machine list -a kalshi-agent-andrew` → expect `started`, 1/1 checks;
  `curl /healthz` → `ok`. If stopped, HTTP probe auto-starts it (auto_start=true).
- **Pause the live agent intentionally** (without stopping the console):
  `flyctl ssh console -a kalshi-agent-andrew -C "pkill -f 'kalshi_agent.cli.*live'"`.
  The watchdog would otherwise restart it in 5s — kill BOTH the watchdog subshell
  and the python process, or use `--restart on-failure` before stopping.
- Live position reconciliation: Kalshi terminal statuses `determined`/`finalized`
  are normalized to `settled` in Market.from_api (fix 2026-11-26); a held market
  that finalized is settled on the next agent cycle from `get_market` — no open
  position on the exchange means the mirror must not hold it either.
  Lost basis is written off via Portfolio.settle with `why="settlement"`.

## CRITICAL — the machine idle-stops after ~300s of no EXTERNAL traffic

Root-caused 2026-11-26 by experiment: the machine is STOPPED by Fly exactly
300 seconds after every start unless EXTERNAL HTTP traffic arrives (Fly's own
30s /healthz checks do NOT count). `auto_stop_machines = "off"` in fly.toml and
min_machines_running=1 do NOT prevent it — Fly honors `requested_stop` regardless
of the machine restart policy (tested with restart=always: still stops). Starts
happen on ANY external HTTP hit (auto_start). Internal boots therefore cascade:
boot → 300s → stop → down until the next visitor/ping.

**Consequence: 24/7 autonomy requires an EXTERNAL ping every < 5 minutes.**

Fix options:
- **cron-job.org** (recommended, free): sign up, add a job →
  URL https://kalshi-agent-andrew.fly.dev/healthz — every **1 minute** (or 4).
- **UptimeRobot** free tier (5-min interval) is borderline vs the 300s idle — prefer cron-job.org.
- The GitHub Actions hourly ping (`.github/workflows/uptime.yml`) remains as a
  backstop only — insufficient alone.

Evidence chain: boots at 14:17:07 → stop 14:22:08 (300.0s); boot 14:29:14 →
stop 14:34:15; boot 14:46:29 → stop 14:51:31 — all requested_stop=true, exit 0.
With external pings every 45s the machine ran past 7 minutes continuously
(healthz 200 throughout, checks 1/1). Fly support ticket re: auto_stop=off
seemingly being overridden is a reasonable escalation.

Also note: `flyctl deploy` resets the machine restart policy to `on-failure`
(it is not expressible in fly.toml) — after ANY deploy, re-apply:
  flyctl machine update <machine-id> --restart always --yes
Deploys also leave the machine stopped; the next external hit auto-starts it.
