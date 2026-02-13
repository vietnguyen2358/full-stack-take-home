# AQ Clone

A website cloning tool that takes a URL, scrapes the page, and uses AI to generate a replica with live preview.

**[Live Site](https://aq-clone-app.vercel.app/)** | **[Demo Video](https://youtu.be/DOw1UEBIEGs)**

## Stack

- **Frontend**: Next.js, TypeScript, Tailwind CSS
- **Backend**: FastAPI, Playwright, OpenRouter
- **Sandbox**: Daytona
- **Database**: Supabase
- **Deployment**: Vercel + Railway

## Running Locally

```bash
# Backend
cd backend
source venv/bin/activate
pip install -r requirements.txt
python -m app.main
# → http://localhost:8000

# Frontend
cd frontend
npm install
npm run dev
# → http://localhost:3000
```

Requires `OPENROUTER_API_KEY`, `DAYTONA_API_KEY`, and `SUPABASE_URL`/`SUPABASE_KEY` in `backend/.env`.
