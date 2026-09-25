from typing import Annotated

from app.db.session import get_db
from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.movies.schemas import GenreOut
from app.movies.service import list_genres

router = APIRouter()


@router.get("", response_model=list[GenreOut])
async def get_genres(db: Annotated[AsyncSession, Depends(get_db)]) -> list[GenreOut]:
    genres = await list_genres(db)
    return [GenreOut.model_validate(genre) for genre in genres]