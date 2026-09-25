"""Schemas Pydantic para leitura e escrita de filmes."""

from datetime import date, datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class MovieListItem(BaseModel):
    """Um filme na listagem paginada, com a média das avaliações dos usuários."""

    model_config = ConfigDict(from_attributes=True)

    sk_movie_id: str
    titulo: str
    ano_lancamento: int | None
    url_poster: str | None
    generos: list[str]
    nota_media: float | None = Field(
        default=None,
        description="Média das notas de movie_reviews; null se o filme não tem avaliações.",
    )
    qtd_avaliacoes: int


class PaginatedMovies(BaseModel):
    """Envelope de paginação para a listagem de filmes."""

    items: list[MovieListItem]
    total: int
    page: int
    page_size: int
    pages: int


class OutrasNotas(BaseModel):
    """Notas de outras fontes. Ausente para filmes cadastrados pela aplicação."""

    nota_tmdb: float | None
    qtd_tmdb: int | None
    nota_imdb: float | None
    qtd_imdb: int | None
    popularidade: float | None
    orcamento_usd: Decimal | None
    receita_usd: Decimal | None


class MovieDetail(BaseModel):
    """Informações completas de um filme: dados básicos, elenco, produtoras e notas."""

    model_config = ConfigDict(from_attributes=True)

    sk_movie_id: str
    id_filme: str
    titulo: str
    data_lancamento: date | None
    ano_lancamento: int | None
    duracao_minutos: int | None
    status_filme: str | None
    sinopse: str | None
    url_poster: str | None
    url_backdrop: str | None
    generos: list[str]
    produtoras: list[str]
    diretores: list[str]
    roteiristas: list[str]
    elenco: list[str]
    nota_media_usuarios: float | None = Field(
        default=None,
        description="Média das notas de movie_reviews; null se o filme não tem avaliações.",
    )
    qtd_avaliacoes_usuarios: int
    outras_notas: OutrasNotas | None = Field(
        default=None, description="null quando o filme não tem linha em fact_movies_performance."
    )

class MovieReviewCreate(BaseModel):
    """Dados para cadastrar uma avaliação de um filme."""

    nome: str = Field(min_length=1, max_length=120)
    nota: float = Field(ge=0, le=10)
    comentario: str = Field(min_length=1, max_length=4000)

    @field_validator("nome", "comentario")
    @classmethod
    def _strip(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("o campo não pode ficar vazio")
        return value


class MovieReviewOut(BaseModel):
    """Uma avaliação (nota + resenha) de um filme."""

    model_config = ConfigDict(from_attributes=True)

    sk_movie_review_id: str
    nome: str
    nota: float
    comentario: str
    created_at: datetime


class PaginatedReviews(BaseModel):
    """Envelope de paginação para a lista de avaliações de um filme."""

    items: list[MovieReviewOut]
    total: int
    page: int
    page_size: int
    pages: int


class GenreOut(BaseModel):
    """Um gênero já registrado, para o front montar a seleção do cadastro."""

    model_config = ConfigDict(from_attributes=True)

    sk_genre_id: str
    nome_genero: str


class MovieCreate(BaseModel):
    """Dados para cadastrar um filme. Diretor e sinopse são opcionais."""

    titulo: str = Field(min_length=1, max_length=500)
    ano_lancamento: int = Field(ge=1870, le=2100)
    data_lancamento: date | None = None
    duracao_minutos: int | None = Field(default=None, gt=0)
    status_filme: str | None = Field(default=None, max_length=50)
    sinopse: str | None = Field(default=None, max_length=4000)
    url_poster: str | None = Field(default=None, max_length=2048)
    url_backdrop: str | None = Field(default=None, max_length=2048)
    diretor: str | None = Field(default=None, min_length=1, max_length=255)
    # IDs de dim_genres (sk_genre_id), não texto livre: o front escolhe entre os já cadastrados.
    generos: list[str] = Field(min_length=1)

    @field_validator("titulo", "diretor", "status_filme", "sinopse")
    @classmethod
    def _strip(cls, value: str | None) -> str | None:
        return value.strip() if value else value

    @field_validator("generos")
    @classmethod
    def _generos_sem_duplicatas(cls, value: list[str]) -> list[str]:
        deduped = list(dict.fromkeys(value))
        if not deduped:
            raise ValueError("informe ao menos um gênero")
        return deduped

    @model_validator(mode="after")
    def _data_condiz_com_ano(self) -> "MovieCreate":
        if self.data_lancamento is not None and self.data_lancamento.year != self.ano_lancamento:
            raise ValueError("data_lancamento não corresponde a ano_lancamento")
        return self