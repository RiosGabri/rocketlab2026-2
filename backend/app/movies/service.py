"""Consultas de leitura e escrita do domínio de filmes."""

from uuid import uuid4

from app.movies.models import DimGenre, DimMovie, DimPerson, MovieReview
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.movies.schemas import (
    MovieCreate,
    MovieDetail,
    MovieListItem,
    MovieReviewCreate,
    MovieReviewOut,
    OutrasNotas,
    PaginatedMovies,
    PaginatedReviews,
)


class MovieWriteError(Exception):
    """Erro de validação de negócio no cadastro ou na atualização de um filme."""


async def list_movies(
    db: AsyncSession,
    page: int,
    page_size: int,
    titulo: str | None = None,
) -> PaginatedMovies:
    """Lista filmes paginados, com a média de `movie_reviews` e os nomes dos gêneros.

    A média é calculada com uma subquery agrupada por filme e unida com LEFT JOIN,
    para não disparar uma consulta por linha (N+1). Filmes sem avaliação recebem
    `nota_media=None` e `qtd_avaliacoes=0`.
    """

    reviews_subquery = (
        select(
            MovieReview.sk_movie_id.label("sk_movie_id"),
            func.avg(MovieReview.nota).label("nota_media"),
            func.count(MovieReview.sk_movie_review_id).label("qtd_avaliacoes"),
        )
        .group_by(MovieReview.sk_movie_id)
        .subquery()
    )

    count_stmt = select(func.count()).select_from(DimMovie)
    stmt = (
        select(DimMovie, reviews_subquery.c.nota_media, reviews_subquery.c.qtd_avaliacoes)
        .outerjoin(reviews_subquery, DimMovie.sk_movie_id == reviews_subquery.c.sk_movie_id)
        .options(selectinload(DimMovie.genres))
        # Desempate por sk_movie_id: com dois filmes de mesmo título, o offset/limit
        # precisa de uma ordem determinística, senão itens repetem ou somem entre páginas.
        .order_by(DimMovie.titulo, DimMovie.sk_movie_id)
    )
    if titulo:
        title_filter = DimMovie.titulo.ilike(f"%{titulo}%")
        count_stmt = count_stmt.where(title_filter)
        stmt = stmt.where(title_filter)

    total = (await db.execute(count_stmt)).scalar_one()

    stmt = stmt.offset((page - 1) * page_size).limit(page_size)

    rows = (await db.execute(stmt)).all()

    items = [
        MovieListItem(
            sk_movie_id=movie.sk_movie_id,
            titulo=movie.titulo,
            ano_lancamento=movie.ano_lancamento,
            url_poster=movie.url_poster,
            generos=[genero.nome_genero for genero in movie.genres],
            nota_media=round(nota_media, 1) if nota_media is not None else None,
            qtd_avaliacoes=qtd_avaliacoes or 0,
        )
        for movie, nota_media, qtd_avaliacoes in rows
    ]

    pages = (total + page_size - 1) // page_size if total else 0
    return PaginatedMovies(items=items, total=total, page=page, page_size=page_size, pages=pages)


async def get_movie_detail(db: AsyncSession, sk_movie_id: str) -> MovieDetail | None:
    """Busca um filme com elenco, produtoras, gêneros e as duas fontes de nota.

    Retorna None quando o filme não existe, para o router responder 404.
    A ordem de diretores/roteiristas/elenco é alfabética: não há coluna de ordem
    de destaque no schema, então uma ordem determinística é preferível a confiar
    na ordem de inserção da tabela.
    """

    stmt = (
        select(DimMovie)
        .options(
            selectinload(DimMovie.genres),
            selectinload(DimMovie.companies),
            selectinload(DimMovie.people),
            selectinload(DimMovie.performance),
        )
        .where(DimMovie.sk_movie_id == sk_movie_id)
    )
    movie = (await db.execute(stmt)).scalar_one_or_none()
    if movie is None:
        return None

    review_stats = (
        await db.execute(
            select(
                func.avg(MovieReview.nota),
                func.count(MovieReview.sk_movie_review_id),
            ).where(MovieReview.sk_movie_id == sk_movie_id)
        )
    ).one()
    nota_media_usuarios, qtd_avaliacoes_usuarios = review_stats

    people_by_type: dict[str, list[str]] = {"Diretor": [], "Roteirista": [], "Ator": []}
    for person in movie.people:
        people_by_type.setdefault(person.tipo_pessoa, []).append(person.nome_pessoa)
    for names in people_by_type.values():
        names.sort()

    outras_notas = None
    if movie.performance is not None:
        outras_notas = OutrasNotas(
            nota_tmdb=movie.performance.nota_tmdb,
            qtd_tmdb=movie.performance.qtd_tmdb,
            nota_imdb=movie.performance.nota_imdb,
            qtd_imdb=movie.performance.qtd_imdb,
            popularidade=movie.performance.popularidade,
            orcamento_usd=movie.performance.orcamento_usd,
            receita_usd=movie.performance.receita_usd,
        )

    return MovieDetail(
        sk_movie_id=movie.sk_movie_id,
        id_filme=movie.id_filme,
        titulo=movie.titulo,
        data_lancamento=movie.data_lancamento,
        ano_lancamento=movie.ano_lancamento,
        duracao_minutos=movie.duracao_minutos,
        status_filme=movie.status_filme,
        sinopse=movie.sinopse,
        url_poster=movie.url_poster,
        url_backdrop=movie.url_backdrop,
        generos=sorted(genero.nome_genero for genero in movie.genres),
        produtoras=sorted(empresa.nome_produtora for empresa in movie.companies),
        diretores=people_by_type["Diretor"],
        roteiristas=people_by_type["Roteirista"],
        elenco=people_by_type["Ator"],
        nota_media_usuarios=(
            round(nota_media_usuarios, 1) if nota_media_usuarios is not None else None
        ),
        qtd_avaliacoes_usuarios=qtd_avaliacoes_usuarios or 0,
        outras_notas=outras_notas,
    )


async def movie_exists(db: AsyncSession, sk_movie_id: str) -> bool:
    """Confere a existência do filme sem carregar suas relações."""

    stmt = select(DimMovie.sk_movie_id).where(DimMovie.sk_movie_id == sk_movie_id)
    return (await db.execute(stmt)).scalar_one_or_none() is not None


async def list_movie_reviews(
    db: AsyncSession,
    sk_movie_id: str,
    page: int,
    page_size: int,
) -> PaginatedReviews | None:
    """Lista as avaliações de um filme, mais recentes primeiro.

    Retorna None quando o filme não existe, para o router responder 404.
    """

    if not await movie_exists(db, sk_movie_id):
        return None

    count_stmt = (
        select(func.count()).select_from(MovieReview).where(MovieReview.sk_movie_id == sk_movie_id)
    )
    total = (await db.execute(count_stmt)).scalar_one()

    stmt = (
        select(MovieReview)
        .where(MovieReview.sk_movie_id == sk_movie_id)
        # created_at pode empatar entre avaliações do seed; o id desempata a ordem.
        .order_by(MovieReview.created_at.desc(), MovieReview.sk_movie_review_id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    reviews = (await db.execute(stmt)).scalars().all()

    pages = (total + page_size - 1) // page_size if total else 0
    return PaginatedReviews(
        items=[MovieReviewOut.model_validate(review) for review in reviews],
        total=total,
        page=page,
        page_size=page_size,
        pages=pages,
    )


async def create_movie_review(
    db: AsyncSession,
    sk_movie_id: str,
    payload: MovieReviewCreate,
) -> MovieReviewOut | None:
    """Cria uma avaliação para um filme existente.

    Retorna None quando o filme não existe.
    """

    if not await movie_exists(db, sk_movie_id):
        return None

    review = MovieReview(
        sk_movie_id=sk_movie_id,
        nome=payload.nome,
        nota=payload.nota,
        comentario=payload.comentario,
    )

    db.add(review)
    await db.commit()
    await db.refresh(review)

    return MovieReviewOut.model_validate(review)


async def list_genres(db: AsyncSession) -> list[DimGenre]:
    """Todos os gêneros cadastrados, para o front montar o seletor do cadastro."""

    stmt = select(DimGenre).order_by(DimGenre.nome_genero)
    return list((await db.execute(stmt)).scalars().all())


async def _get_or_create_director(db: AsyncSession, nome: str) -> DimPerson:
    """Reaproveita o diretor se já existir (mesmo nome e tipo); senão cria um novo.

    Reaproveitar a mesma linha, em vez de duplicar, é o que faz uma futura correção
    do nome do diretor valer para todos os filmes dele automaticamente.
    """

    stmt = select(DimPerson).where(
        DimPerson.nome_pessoa == nome, DimPerson.tipo_pessoa == "Diretor"
    )
    existing = (await db.execute(stmt)).scalar_one_or_none()
    if existing is not None:
        return existing
    return DimPerson(nome_pessoa=nome, tipo_pessoa="Diretor")


async def _generate_unique_id_filme(db: AsyncSession) -> str:
    """UUID para id_filme, conferindo (sem confiar apenas na probabilidade) que é inédito."""

    for _ in range(5):
        candidate = str(uuid4())
        stmt = select(DimMovie.id_filme).where(DimMovie.id_filme == candidate)
        if (await db.execute(stmt)).scalar_one_or_none() is None:
            return candidate
    raise MovieWriteError("não foi possível gerar um id_filme inédito; tente novamente")


async def create_movie(db: AsyncSession, payload: MovieCreate) -> MovieDetail:
    """Cadastra um filme: get-or-create do diretor, gêneros validados por id.

    O filme criado não recebe linha em fact_movies_performance nem em dim_reviews;
    `outras_notas` e `nota_media_usuarios` do resultado vêm null até a primeira avaliação.
    """

    genre_stmt = select(DimGenre).where(DimGenre.sk_genre_id.in_(payload.generos))
    genres = list((await db.execute(genre_stmt)).scalars().all())
    found_ids = {genre.sk_genre_id for genre in genres}
    missing_ids = set(payload.generos) - found_ids
    if missing_ids:
        raise MovieWriteError(f"gêneros inexistentes: {sorted(missing_ids)}")

    movie = DimMovie(
        id_filme=await _generate_unique_id_filme(db),
        titulo=payload.titulo,
        data_lancamento=payload.data_lancamento,
        ano_lancamento=payload.ano_lancamento,
        duracao_minutos=payload.duracao_minutos,
        status_filme=payload.status_filme,
        sinopse=payload.sinopse,
        url_poster=payload.url_poster,
        url_backdrop=payload.url_backdrop,
        genres=genres,
    )
    if payload.diretor:
        movie.people.append(await _get_or_create_director(db, payload.diretor))

    db.add(movie)
    try:
        await db.commit()
    except IntegrityError as error:
        await db.rollback()
        raise MovieWriteError(
            "não foi possível cadastrar o filme (dado duplicado ou inválido)"
        ) from error

    detail = await get_movie_detail(db, movie.sk_movie_id)
    assert detail is not None  # acabou de ser commitado nesta mesma sessão
    return detail


async def _sync_director(db: AsyncSession, movie: DimMovie, novo_nome: str | None) -> None:
    """Ajusta o diretor do filme sem tocar em elenco/roteiristas (mesma relação `people`).

    Três casos, na ordem:
    1. Sem mudança: o nome já é o do único diretor atual.
    2. Já existe OUTRA pessoa (Diretor) com esse nome: troca o vínculo para ela
       (reatribuição a um diretor já cadastrado).
    3. Não existe: se o filme já tinha um único diretor, corrige o nome dessa MESMA
       linha (efeito cascata em todos os filmes dela); senão, cria um diretor novo.
    """

    atuais = [pessoa for pessoa in movie.people if pessoa.tipo_pessoa == "Diretor"]

    if novo_nome is None:
        for pessoa in atuais:
            movie.people.remove(pessoa)
        return

    if len(atuais) == 1 and atuais[0].nome_pessoa == novo_nome:
        return

    stmt = select(DimPerson).where(
        DimPerson.nome_pessoa == novo_nome, DimPerson.tipo_pessoa == "Diretor"
    )
    outra_pessoa = (await db.execute(stmt)).scalar_one_or_none()

    if outra_pessoa is not None:
        for pessoa in atuais:
            movie.people.remove(pessoa)
        if outra_pessoa not in movie.people:
            movie.people.append(outra_pessoa)
        return

    if len(atuais) == 1:
        atuais[0].nome_pessoa = novo_nome
        return

    for pessoa in atuais:
        movie.people.remove(pessoa)
    movie.people.append(DimPerson(nome_pessoa=novo_nome, tipo_pessoa="Diretor"))


async def update_movie(
    db: AsyncSession, sk_movie_id: str, payload: MovieCreate
) -> MovieDetail | None:
    """Atualiza um filme (substituição completa dos campos editáveis e dos gêneros).

    `id_filme` nunca muda. Retorna None quando o filme não existe, para 404.
    Produtoras não são tocadas: este formulário não as edita.
    """

    stmt = (
        select(DimMovie)
        .options(selectinload(DimMovie.genres), selectinload(DimMovie.people))
        .where(DimMovie.sk_movie_id == sk_movie_id)
    )
    movie = (await db.execute(stmt)).scalar_one_or_none()
    if movie is None:
        return None

    genre_stmt = select(DimGenre).where(DimGenre.sk_genre_id.in_(payload.generos))
    genres = list((await db.execute(genre_stmt)).scalars().all())
    missing_ids = set(payload.generos) - {genre.sk_genre_id for genre in genres}
    if missing_ids:
        raise MovieWriteError(f"gêneros inexistentes: {sorted(missing_ids)}")

    movie.titulo = payload.titulo
    movie.data_lancamento = payload.data_lancamento
    movie.ano_lancamento = payload.ano_lancamento
    movie.duracao_minutos = payload.duracao_minutos
    movie.status_filme = payload.status_filme
    movie.sinopse = payload.sinopse
    movie.url_poster = payload.url_poster
    movie.url_backdrop = payload.url_backdrop
    movie.genres = genres
    await _sync_director(db, movie, payload.diretor)

    try:
        await db.commit()
    except IntegrityError as error:
        await db.rollback()
        raise MovieWriteError(
            "não foi possível atualizar o filme (dado duplicado ou inválido)"
        ) from error

    return await get_movie_detail(db, sk_movie_id)


async def delete_movie(db: AsyncSession, sk_movie_id: str) -> bool:
    """Remove um filme e o que só existe em função dele (avaliações, fato, vínculos).

    Gêneros, pessoas e produtoras não são apagados: outros filmes podem usá-los.
    Retorna False quando o filme não existe, para o router responder 404.
    """

    movie = (
        await db.execute(select(DimMovie).where(DimMovie.sk_movie_id == sk_movie_id))
    ).scalar_one_or_none()
    if movie is None:
        return False
    await db.delete(movie)
    await db.commit()
    return True