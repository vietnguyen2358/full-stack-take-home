import logging
from pathlib import Path

from dotenv import load_dotenv

# Explicitly point to the .env file relative to this file's location
_env_path = Path(__file__).resolve().parent.parent / ".env"
load_dotenv(_env_path)

# Configure logging before importing anything else
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from app.routes.example import router as example_router
from app.routes.clone import router as clone_router

app = FastAPI(title="Backend API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(example_router)
app.include_router(clone_router)

@app.get("/")
def root():
    return {"message": "Backend is running 🚀"}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app.main:app", host="0.0.0.0", port=8000, reload=True)
