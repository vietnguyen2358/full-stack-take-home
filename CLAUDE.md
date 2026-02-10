# Website Cloning Tool

## Project Overview
A website cloning tool (similar to orchids.app / Same.new) that takes a URL input, clones the website, and displays it in a sandbox.

## Stack
- **Frontend**: Next.js 16.1.6 (TypeScript, Tailwind CSS, App Router) — `frontend/`
- **Backend**: FastAPI (Python) — `backend/`
- **Database**: Supabase
- **AI**: OpenRouter for model calls
- **Sandboxing**: Daytona (for website preview sandboxes)
- **UI Components**: shadcn

## Project Structure
```
frontend/          → Next.js app (port 3000)
  src/app/         → App Router pages
backend/           → FastAPI app (port 8000)
  app/main.py      → Entry point (includes uvicorn runner)
  app/routes/      → API route modules
  app/database.py  → DB connection (placeholder)
  venv/            → Python virtual environment
db/                → SQL schema / migrations
```

## Running Locally
```bash
# Backend
cd backend
source venv/bin/activate
python -m app.main
# → http://localhost:8000

# Frontend
cd frontend
npm run dev
# → http://localhost:3000
```

## Key Endpoints
- `GET /` — Backend health check
- `GET /example` — Example route (test endpoint)

## Deployment
- Frontend: Vercel
- Backend: Railway or AWS/GCP

## Required Features
- [ ] Text box to input website URL to clone
- [ ] Sandbox to display the cloned website
- [ ] Deploy full project on Vercel
- [ ] Video demo of cloning tool in README
- [ ] Deployed project link in README

## Conventions
- Backend venv at `backend/venv/` — always activate before running
- CORS is open (allow all origins) for dev
- Use `app.include_router()` in main.py to register new route modules
