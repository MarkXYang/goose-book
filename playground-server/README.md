# Remote Goose Playground Runner

This directory contains a hardened, self-contained Dockerized HTTP runner (`GET /api/health`, `POST /api/run`) that compiles and executes Goose snippets from anywhere on the web.

Combined with the book's built-in static snapshot cache (`theme/goose-precomputed.js`), readers get:
- **Zero-wait execution everywhere** for every unedited book listing (even offline or if the backend is cold/unconfigured), **and**
- **Full live execution of edited code** (including `thread_fn` native builds and `goose check --stacks`) once this runner is deployed.

## Security Model

- Runs as an unprivileged `runner` user inside an isolated Linux container.
- Enforces per-request POSIX limits (`RLIMIT_CPU`, `RLIMIT_FSIZE`, `RLIMIT_NOFILE`), wall-clock timeouts with process-group termination (`os.killpg`), bounded concurrency (`MAX_CONCURRENT=4`), and per-IP sliding-window rate limiting (`RATE_LIMIT_PER_MIN=30`).
- Rejects `extern` declarations and `embed_file(...)` in untrusted user submissions by default (`ALLOW_EXTERN=0`).
- Runs with a scrubbed environment (`PATH`, `HOME=/tmp`, `GOOSE_HOME=/tmp/goose_home`) and ephemeral per-request temp directories.

## Quick Deploy Options

### Option A: Fly.io (scale-to-zero, fast cold starts)

```bash
cd playground-server
fly launch --copy-config --no-deploy
fly deploy
# Note your HTTPS URL, e.g. https://goose-book-playground.fly.dev
```

### Option B: Google Cloud Run (serverless, scale-to-zero)

```bash
cd playground-server
gcloud run deploy goose-book-playground \
  --source . \
  --region us-central1 \
  --memory 1Gi \
  --cpu 1 \
  --concurrency 4 \
  --allow-unauthenticated
```

### Option C: Render.com (free tier from GitHub)

Create a new **Blueprint** or **Web Service** pointing at `playground-server/Dockerfile` (or use `playground-server/render.yaml`).

### Option D: Any Docker Host / VPS

```bash
cd playground-server
docker build -t goose-playground .
docker run -d --name goose-playground \
  --restart unless-stopped \
  --memory 1g --cpus 1 --pids-limit 128 \
  --read-only --tmpfs /tmp:rw,exec,size=256m \
  --network bridge \
  -p 8080:8080 \
  goose-playground
```

*(Note: `--tmpfs /tmp:rw,exec` is required so `goose build` can execute compiled native binaries for `thread_fn` examples.)*

## Connecting the Published Book

Once deployed, connect your static book to the runner in either of two ways:

1. **Via GitHub Actions (Recommended for GitHub Pages):**
   In your repository **Settings → Secrets and variables → Actions → Variables**, add a variable named:
   - `GOOSE_PLAYGROUND_URL` = `https://your-runner-url.example.com`

   The included `.github/workflows/pages.yml` automatically injects it into `theme/goose-config.js` on every push to `main`.

2. **Directly in `theme/goose-config.js`:**
   ```js
   window.GOOSE_PLAYGROUND_URL = "https://your-runner-url.example.com";
   ```

Readers can also override or test any runner URL live in their browser by appending `?runner=https://...` to the URL or using the inline **Configure runner** bar in any code block's output panel.
