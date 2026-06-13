# Deploying Data Pipeline Copilot for free

The app has two pieces that deploy separately:

- **Frontend** (Next.js) → **Vercel** free Hobby tier (best place for Next.js)
- **Backend** (FastAPI) → a free Python host (Render, Hugging Face Spaces, or Vercel)

The backend is **stateless-safe**: follow-up requests (impact simulation, "Explain this") re-analyze from the submitted code if the in-memory cache misses, so it runs correctly on any host — single-container or serverless. Nothing ever needs a database.

All paths below are free and need **no credit card**.

---

## Step 0 — Put the code on GitHub (once)

Every free host below deploys by connecting a GitHub repo.

```powershell
cd c:\Users\jaina\Desktop\Data_Intelligence
git add -A
git commit -m "Data Pipeline Copilot"
# create an empty repo on github.com first, then:
git remote add origin https://github.com/<you>/data-pipeline-copilot.git
git branch -M main
git push -u origin main
```

`.env` files are git-ignored, so your keys never get pushed.

---

## Path A — Vercel (frontend) + Render (backend)  ·  recommended, simplest

### A1. Backend on Render
1. Go to https://render.com → sign in with GitHub (free, no card).
2. **New + → Blueprint**, pick your repo. Render reads [render.yaml](render.yaml) and provisions the API on the free plan automatically. (Or **New + → Web Service**, root directory `backend`, build `pip install -r requirements.txt`, start `uvicorn main:app --host 0.0.0.0 --port $PORT`.)
3. Note the URL it gives you, e.g. `https://pipeline-copilot-api.onrender.com`.
4. (Do CORS after the frontend exists — see **A3**.)

> Free Render web services sleep after ~15 min idle and cold-start in ~30–50s on the next request. Fine for a demo; see Path B for always-on.

### A2. Frontend on Vercel
1. Go to https://vercel.com → sign in with GitHub (free, no card).
2. **Add New → Project**, import your repo.
3. Set **Root Directory** to `frontend`.
4. Add an environment variable:
   - `NEXT_PUBLIC_API_URL` = your Render backend URL from A1 (e.g. `https://pipeline-copilot-api.onrender.com`)
5. **Deploy.** You get a URL like `https://pipeline-copilot.vercel.app`.

> `NEXT_PUBLIC_API_URL` is baked in at build time — if you change it later, redeploy the frontend.

### A3. Connect them (CORS)
On Render, set the backend's `CORS_ORIGINS` env var to your exact Vercel URL (Settings → Environment), then redeploy:
```
CORS_ORIGINS=https://pipeline-copilot.vercel.app
```
That's it — open the Vercel URL and it's live for anyone.

---

## Path B — Backend on Hugging Face Spaces (always-on, fully free)

No sleep/cold-start, no card. Uses the [backend/Dockerfile](backend/Dockerfile).

1. https://huggingface.co → create account → **New Space**.
2. SDK = **Docker**, blank template, public.
3. In the Space, add the contents of your `backend/` folder (push to the Space's git repo, or upload).
4. Add a `README.md` **in the Space** with this frontmatter so HF routes to the right port:
   ```
   ---
   title: Pipeline Copilot API
   sdk: docker
   app_port: 8000
   ---
   ```
5. In **Settings → Variables and secrets**, set `CORS_ORIGINS` to your frontend URL (and optionally an LLM key — see below).
6. The Space builds the Docker image and serves at `https://<you>-<space>.hf.space`. Use that as `NEXT_PUBLIC_API_URL` on Vercel.

---

## Path C — Everything on Vercel (one platform)

Deploy **two Vercel projects** from the same repo:

- **Frontend project** — Root Directory `frontend`, env `NEXT_PUBLIC_API_URL` = the backend project's URL.
- **Backend project** — Root Directory `backend`. Vercel detects Python via [backend/requirements.txt](backend/requirements.txt) and serves the FastAPI app through [backend/api/index.py](backend/api/index.py) + [backend/vercel.json](backend/vercel.json) (already included). Set env `CORS_ORIGINS` to the frontend URL.

Vercel runs Python on Fluid Compute, scales to zero, and has no cold-start tax like free containers. This is the cleanest long-term setup; it works because the backend is stateless.

---

## Turning on live LLM explanations (optional)

Without a provider, explanations use the built-in offline fallback and everything else is exact local math. To get live LLM reasoning for visitors, set two env vars **on the backend host** (Render/HF/Vercel dashboard — never commit keys):

```
LLM_PROVIDER=groq
GROQ_API_KEY=gsk_...        # free key, no card, from https://console.groq.com
```

(Or `gemini` / `openrouter` — see [backend/.env.example](backend/.env.example).) The key lives only on the server; it's never shipped to the browser.

> Free LLM tiers are rate-limited (e.g. Groq ~1,000 req/day shared across **all** visitors). When the limit is hit the app automatically falls back to offline explanations, so it never breaks.

---

## Good to know before going public

- **It's safe to expose.** The parsers do static analysis only (`sqlglot` + Python `ast`) — pasted code is never executed, so there's no code-execution risk.
- **Abuse:** anyone can hit the analyze endpoint. Vercel includes basic DDoS protection; add a rate limit (e.g. Vercel BotID, or a simple per-IP limiter) before a real public launch.
- **Custom domain:** both Vercel and Render let you attach a custom domain free.
- **Auto-deploys:** once connected, every `git push` to `main` redeploys automatically.

### Quick recommendation
- Easiest free demo: **Path A** (Vercel + Render).
- Always-on free: **Path B** (Vercel + Hugging Face Spaces).
- One platform, cleanest: **Path C** (all Vercel).
