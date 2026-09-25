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