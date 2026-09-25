from fastapi import APIRouter

from app.api.v1.genres import router as genres_router
from app.api.v1.movies import router as movies_router

api_router = APIRouter()

api_router.include_router(movies_router, prefix="/movies", tags=["movies"])
api_router.include_router(genres_router, prefix="/genres", tags=["genres"])
