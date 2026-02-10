from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from app.routes.example import router as example_router

app = FastAPI(title="Backend API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(example_router)

@app.get("/")
def root():
    return {"message": "Backend is running 🚀"}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app.main:app", host="0.0.0.0", port=8000, reload=True)
