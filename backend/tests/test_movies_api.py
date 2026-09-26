from sqlalchemy.exc import IntegrityError
from app.movies.models import DimGenre

async def test_get_movie_not_found(client):
    assert (await client.get("/api/v1/movies/nao-existe")).status_code == 404


async def test_post_review_movie_not_found(client):
    response = await client.post(
        "/api/v1/movies/nao-existe/reviews",
        json={"nome": "Fulano", "nota": 8, "comentario": "Bom"},
    )
    assert response.status_code == 404


async def test_list_reviews_movie_not_found(client):
    assert (await client.get("/api/v1/movies/nao-existe/reviews")).status_code == 404

async def _create_genre(db_session, nome: str = "Drama") -> DimGenre:
    genre = DimGenre(nome_genero=nome)
    db_session.add(genre)
    await db_session.commit()
    await db_session.refresh(genre)
    return genre

async def test_post_movie_success(client, db_session):
    genre = await _create_genre(db_session)
    payload = {
        "titulo": "Filme API",
        "ano_lancamento": 2021,
        "generos": [genre.sk_genre_id],
        "diretor": "Ana Diretora",
    }
    response = await client.post("/api/v1/movies", json=payload)

    assert response.status_code == 201
    body = response.json()
    assert response.headers["location"] == f"/api/v1/movies/{body['sk_movie_id']}"
    assert body["titulo"] == "Filme API"
    assert body["diretores"] == ["Ana Diretora"]
    assert body["generos"] == ["Drama"]


async def test_post_movie_invalid_genre_returns_422(client):
    payload = {"titulo": "X", "ano_lancamento": 2021, "generos": ["nao-existe"]}
    response = await client.post("/api/v1/movies", json=payload)
    assert response.status_code == 422


async def test_post_movie_conflict_returns_409(client, db_session, monkeypatch):
    from app.movies import service

    genre = await _create_genre(db_session)
    payload = {"titulo": "Primeiro", "ano_lancamento": 2020, "generos": [genre.sk_genre_id]}
    first = await client.post("/api/v1/movies", json=payload)
    assert first.status_code == 201
    first_id_filme = first.json()["id_filme"]

    async def fake_unique_id(_db: object) -> str:
        return first_id_filme  # reaproveita um id já usado, pulando a checagem real

    monkeypatch.setattr(service, "_generate_unique_id_filme", fake_unique_id)

    payload2 = {"titulo": "Segundo", "ano_lancamento": 2020, "generos": [genre.sk_genre_id]}
    second = await client.post("/api/v1/movies", json=payload2)
    assert second.status_code == 409


async def test_put_movie_success(client, db_session):
    genre = await _create_genre(db_session)
    created = await client.post(
        "/api/v1/movies",
        json={
            "titulo": "Original",
            "ano_lancamento": 2020,
            "generos": [genre.sk_genre_id],
            "diretor": "Ana",
        },
    )
    sk_movie_id = created.json()["sk_movie_id"]

    response = await client.put(
        f"/api/v1/movies/{sk_movie_id}",
        json={
            "titulo": "Atualizado",
            "ano_lancamento": 2021,
            "generos": [genre.sk_genre_id],
            "diretor": "Bruno",
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["titulo"] == "Atualizado"
    assert body["diretores"] == ["Bruno"]


async def test_put_movie_not_found_returns_404(client):
    payload = {"titulo": "X", "ano_lancamento": 2020, "generos": ["qualquer"]}
    response = await client.put("/api/v1/movies/nao-existe", json=payload)
    assert response.status_code == 404


async def test_put_movie_conflict_returns_409(client, db_session, monkeypatch):
    genre = await _create_genre(db_session)
    created = await client.post(
        "/api/v1/movies",
        json={"titulo": "Original", "ano_lancamento": 2020, "generos": [genre.sk_genre_id]},
    )
    sk_movie_id = created.json()["sk_movie_id"]

    async def broken_commit() -> None:
        raise IntegrityError("stmt", {}, Exception("dup"))

    monkeypatch.setattr(db_session, "commit", broken_commit)

    response = await client.put(
        f"/api/v1/movies/{sk_movie_id}",
        json={"titulo": "Novo", "ano_lancamento": 2020, "generos": [genre.sk_genre_id]},
    )
    assert response.status_code == 409


async def test_delete_movie_success(client, db_session):
    genre = await _create_genre(db_session)
    created = await client.post(
        "/api/v1/movies",
        json={"titulo": "Para apagar", "ano_lancamento": 2020, "generos": [genre.sk_genre_id]},
    )
    sk_movie_id = created.json()["sk_movie_id"]

    response = await client.delete(f"/api/v1/movies/{sk_movie_id}")
    assert response.status_code == 204

    follow_up = await client.get(f"/api/v1/movies/{sk_movie_id}")
    assert follow_up.status_code == 404


async def test_delete_movie_not_found_returns_404(client):
    response = await client.delete("/api/v1/movies/nao-existe")
    assert response.status_code == 404



async def test_post_movie_review_success(client, db_session):
    genre = await _create_genre(db_session)
    created = await client.post(
        "/api/v1/movies",
        json={"titulo": "Filme Avaliado", "ano_lancamento": 2020, "generos": [genre.sk_genre_id]},
    )
    sk_movie_id = created.json()["sk_movie_id"]

    response = await client.post(
        f"/api/v1/movies/{sk_movie_id}/reviews",
        json={"nome": "Fulano", "nota": 8, "comentario": "Muito bom"},
    )
    assert response.status_code == 201
    assert response.json()["nota"] == 8

    detail = (await client.get(f"/api/v1/movies/{sk_movie_id}")).json()
    assert detail["nota_media_usuarios"] == 8.0
    assert detail["qtd_avaliacoes_usuarios"] == 1