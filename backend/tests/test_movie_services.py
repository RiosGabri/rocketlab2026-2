import pytest
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.movies.models import DimGenre, DimMovie, DimPerson
from app.movies.schemas import MovieCreate
from app.movies.service import (
    MovieConflictError,
    MovieWriteError,
    _generate_unique_id_filme,
    _sync_director,
    create_movie,
    get_movie_detail,
)


async def _make_genre(db_session, nome="Drama") -> DimGenre:
    genre = DimGenre(nome_genero=nome)
    db_session.add(genre)
    await db_session.commit()
    await db_session.refresh(genre)
    return genre


def _payload(genre_id: str, diretor: str | None, titulo: str = "Filme Teste") -> MovieCreate:
    return MovieCreate(titulo=titulo, ano_lancamento=2020, generos=[genre_id], diretor=diretor)


async def test_create_movie_invalid_genre_raises(db_session):
    with pytest.raises(MovieWriteError):
        await create_movie(db_session, _payload("id-inexistente", diretor=None))


async def test_create_movie_reuses_existing_director(db_session):
    genre = await _make_genre(db_session)
    payload = _payload(genre.sk_genre_id, diretor="Ana Diretora")

    first = await create_movie(db_session, payload)
    second = await create_movie(
        db_session, _payload(genre.sk_genre_id, "Ana Diretora", "Outro título"))

    assert first.diretores == second.diretores == ["Ana Diretora"]
    directors = (
        await db_session.execute(select(DimPerson).where(DimPerson.tipo_pessoa == "Diretor"))
    ).scalars().all()
    assert len(directors) == 1  # não duplicou a linha


async def test_generate_unique_id_filme_avoids_collision(db_session, monkeypatch):
    from app.movies import service

    used = iter(["id-repetido", "id-repetido", "id-novo"])
    monkeypatch.setattr(service, "uuid4", lambda: next(used))
    db_session.add(DimMovie(id_filme="id-repetido", titulo="Já existe"))
    await db_session.commit()

    assert await _generate_unique_id_filme(db_session) == "id-novo"


async def test_create_movie_raises_conflict_on_integrity_error(db_session, monkeypatch):
    from app.movies import service

    genre = await _make_genre(db_session)
    payload = _payload(genre.sk_genre_id, diretor=None)
    first = await create_movie(db_session, payload)

    async def fake_unique_id(_db: object) -> str:
        return first.id_filme  # reaproveita um id já usado, pulando a checagem real

    monkeypatch.setattr(service, "_generate_unique_id_filme", fake_unique_id)

    with pytest.raises(MovieConflictError):
        await create_movie(db_session, payload)


async def test_sync_director_rename_cascades_to_other_movies(db_session):
    genre = await _make_genre(db_session)
    movie_a = await create_movie(db_session, _payload(genre.sk_genre_id, "Ana", "Filme A"))
    await create_movie(db_session, _payload(genre.sk_genre_id, "Ana", "Filme B"))

    stmt = (
        select(DimMovie)
        .options(selectinload(DimMovie.people))
        .where(DimMovie.sk_movie_id == movie_a.sk_movie_id)
    )
    movie_a_orm = (await db_session.execute(stmt)).scalar_one()

    await _sync_director(db_session, movie_a_orm, "Ana Renomeada")
    await db_session.commit()

    detail_b = await get_movie_detail(
        db_session, (await get_movie_detail(db_session, movie_a.sk_movie_id)).sk_movie_id)
    assert detail_b.diretores == ["Ana Renomeada"]

async def test_sync_director_no_change_is_noop(db_session):
    genre = await _make_genre(db_session)
    movie = await create_movie(db_session, _payload(genre.sk_genre_id, "Ana"))

    stmt = (
        select(DimMovie)
        .options(selectinload(DimMovie.people))
        .where(DimMovie.sk_movie_id == movie.sk_movie_id)
    )
    movie_orm = (await db_session.execute(stmt)).scalar_one()
    original_person_id = next(
        p.sk_person_id for p in movie_orm.people if p.tipo_pessoa == "Diretor"
    )

    await _sync_director(db_session, movie_orm, "Ana")
    await db_session.commit()

    directors = (
        await db_session.execute(select(DimPerson).where(DimPerson.tipo_pessoa == "Diretor"))
    ).scalars().all()
    assert len(directors) == 1
    assert directors[0].sk_person_id == original_person_id  # mesma linha, não recriou


async def test_sync_director_reassigns_to_existing_person(db_session):
    genre = await _make_genre(db_session)
    movie_a = await create_movie(db_session, _payload(genre.sk_genre_id, "Ana", "Filme A"))
    await create_movie(db_session, _payload(genre.sk_genre_id, "Bruno", "Filme B"))

    stmt = (
        select(DimMovie)
        .options(selectinload(DimMovie.people))
        .where(DimMovie.sk_movie_id == movie_a.sk_movie_id)
    )
    movie_a_orm = (await db_session.execute(stmt)).scalar_one()

    await _sync_director(db_session, movie_a_orm, "Bruno")
    await db_session.commit()

    detail_a = await get_movie_detail(db_session, movie_a.sk_movie_id)
    assert detail_a.diretores == ["Bruno"]

    directors = (
        await db_session.execute(select(DimPerson).where(DimPerson.tipo_pessoa == "Diretor"))
    ).scalars().all()
    assert len(directors) == 2  # reaproveitou o Bruno existente, não criou um terceiro
    assert {d.nome_pessoa for d in directors} == {"Ana", "Bruno"}