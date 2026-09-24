"""Carga dos CSVs do RocketLab no banco SQLite.

Uso (dentro de backend/, depois de `alembic upgrade head`):
    python -m app.scripts.seed --limit 4000 --reset   -> subconjunto dos filmes mais votados
    python -m app.scripts.seed --reset                -> carga completa
Os CSVs são procurados (recursivamente) em backend/data/ ou no diretório passado
em --data-dir, então os zips podem ser extraídos ali do jeito que vieram.
"""

import argparse
import csv
import sqlite3
import sys
from collections.abc import Callable, Iterator
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy import (
    Column,
    Date,
    Float,
    Integer,
    Numeric,
    String,
    Table,
    create_engine,
    func,
    inspect,
    select,
)
from sqlalchemy.engine import Connection

from app.core.config import get_settings
from app.db.base import Base
from app.movies import models  # noqa: F401  Registra as tabelas no metadata.

DEFAULT_DATA_DIR = Path(__file__).resolve().parents[2] / "data"
BATCH_SIZE = 5000

# nome do arquivo CSV (sem extensão) -> tabela de destino
CSV_TO_TABLE = {
    "dim_movies": "dim_movies",
    "dim_genres": "dim_genres",
    "dim_companies": "dim_companies",
    "dim_people": "dim_people",
    "bridge_movie_genre": "bridge_movie_genre",
    "bridge_movie_company": "bridge_movie_company",
    "bridge_movie_person": "bridge_movie_person",
    "fact_movies_performance": "fact_movies_performance",
    "movies_reviews": "movie_reviews",
}

# Filhos antes dos pais, para limpar sem depender de chaves estrangeiras.
DELETE_ORDER = [
    "movie_reviews",
    "fact_movies_performance",
    "bridge_movie_person",
    "bridge_movie_company",
    "bridge_movie_genre",
    "dim_reviews",
    "dim_people",
    "dim_companies",
    "dim_genres",
    "dim_movies",
]

Row = dict[str, str]
RowFilter = Callable[[Row], bool]
RowHook = Callable[[Row], object]


class SeedError(Exception):
    """Erro esperado de uso ou de dados, mostrado sem traceback."""


def find_csvs(data_dir: Path) -> dict[str, Path]:
    """Localiza cada CSV esperado dentro de data_dir (busca recursiva)."""

    if not data_dir.is_dir():
        raise SeedError(f"Diretório de dados não encontrado: {data_dir}")

    found: dict[str, Path] = {}
    missing: list[str] = []
    for name in CSV_TO_TABLE:
        matches = [
            path for path in sorted(data_dir.rglob(f"{name}.csv")) if "__MACOSX" not in path.parts
        ]
        if not matches:
            missing.append(f"{name}.csv")
        elif len(matches) > 1:
            listed = ", ".join(str(path) for path in matches)
            raise SeedError(f"Mais de um {name}.csv em {data_dir}: {listed}")
        else:
            found[name] = matches[0]

    if missing:
        raise SeedError(
            f"CSVs ausentes em {data_dir}: {', '.join(missing)}. "
            "Extraia os zips dos dados nessa pasta (ou use --data-dir)."
        )
    return found


def read_header(path: Path) -> list[str]:
    with path.open(newline="", encoding="utf-8-sig") as file:
        return next(csv.reader(file), [])


def read_rows(path: Path) -> Iterator[Row]:
    with path.open(newline="", encoding="utf-8-sig") as file:
        yield from csv.DictReader(file)


def make_converter(column: Column[Any]) -> Callable[[str], Any]:
    """Converte o texto do CSV para o tipo Python da coluna."""

    column_type = column.type
    if isinstance(column_type, Integer):
        return lambda value: int(float(value))  # aceita "120" e "120.0"
    if isinstance(column_type, Date):
        return date.fromisoformat
    if isinstance(column_type, Float):  # inclui Double; precisa vir antes de Numeric
        return float
    if isinstance(column_type, Numeric):
        return Decimal
    return str


def empty_value(column: Column[Any]) -> Any:
    """Valor usado quando a célula do CSV está vazia."""

    if column.nullable:
        return None
    if column.default is not None and column.default.is_scalar:
        return column.default.arg
    if isinstance(column.type, String):
        return ""
    return None


def load_table(
    conn: Connection,
    path: Path,
    table: Table,
    keep: RowFilter | None = None,
    collect: RowHook | None = None,
) -> int:
    """Insere em lotes as linhas do CSV que passam por `keep`; devolve quantas entraram."""

    header = read_header(path)
    columns = {column.name: column for column in table.columns}

    extras = [name for name in header if name not in columns]
    if extras:
        print(f"  aviso: colunas de {path.name} ignoradas (não existem na tabela): {extras}")

    required = [
        column.name
        for column in table.columns
        if not column.nullable
        and column.default is None
        and column.server_default is None
        and column.name not in header
    ]
    if required:
        raise SeedError(
            f"{path.name} não tem as colunas obrigatórias de {table.name}: {required}. "
            "Confira se está usando a versão atual dos CSVs."
        )

    converters = {name: make_converter(columns[name]) for name in header if name in columns}

    def convert(row: Row) -> dict[str, Any]:
        return {
            name: (convert_value(row[name]) if row[name] != "" else empty_value(columns[name]))
            for name, convert_value in converters.items()
        }

    inserted = 0
    batch: list[dict[str, Any]] = []
    for row in read_rows(path):
        if keep is not None and not keep(row):
            continue
        if collect is not None:
            collect(row)
        batch.append(convert(row))
        if len(batch) >= BATCH_SIZE:
            conn.execute(table.insert(), batch)
            inserted += len(batch)
            batch = []
    if batch:
        conn.execute(table.insert(), batch)
        inserted += len(batch)
    return inserted


def in_set(column: str, ids: set[str] | None) -> RowFilter | None:
    """Filtro que aceita só linhas cujo `column` está em `ids` (None = sem filtro)."""

    if ids is None:
        return None
    return lambda row: row[column] in ids


def collector(column: str, sink: set[str]) -> RowHook:
    return lambda row: sink.add(row[column])


def select_top_movies(files: dict[str, Path], limit: int) -> set[str]:
    """Os `limit` filmes com mais votos no TMDB (desempate por popularidade e id)."""

    existing = {row["sk_movie_id"] for row in read_rows(files["dim_movies"])}

    def number(value: str | None) -> float:
        return float(value) if value else -1.0  # sem voto vai para o fim do ranking

    ranking = [
        (number(row.get("qtd_tmdb")), number(row.get("popularidade")), row["sk_movie_id"])
        for row in read_rows(files["fact_movies_performance"])
        if row["sk_movie_id"] in existing
    ]
    ranking.sort(reverse=True)
    return {movie_id for _, _, movie_id in ranking[:limit]}


def run(data_dir: Path, limit: int | None, reset: bool) -> None:
    files = find_csvs(data_dir)
    tables = Base.metadata.tables

    # O Alembic roda de forma síncrona; aqui também. Sem o listener de PRAGMA da
    # aplicação, as FKs ficam desligadas durante a carga (ordem livre e mais rápido);
    # a integridade é conferida com foreign_key_check antes do commit.
    database_url = get_settings().database_url.replace("+aiosqlite", "")
    engine = create_engine(database_url)
    db_file = Path(engine.url.database or "").resolve()
    print(f"Banco: {db_file}")

    missing_tables = set(CSV_TO_TABLE.values()) - set(inspect(engine).get_table_names())
    if missing_tables:
        raise SeedError(
            f"Tabelas ausentes: {sorted(missing_tables)}. Rode `alembic upgrade head` antes."
        )

    with engine.connect() as conn:
        already_loaded = conn.scalar(select(func.count()).select_from(tables["dim_movies"]))
    if already_loaded and not reset:
        raise SeedError(
            f"O banco já tem {already_loaded} filmes. Use --reset para limpar e recarregar."
        )

    selected: set[str] | None = None
    if limit is not None:
        selected = select_top_movies(files, limit)
        print(f"Selecionados {len(selected)} filmes (mais votados no TMDB).")

    person_ids: set[str] = set()
    company_ids: set[str] = set()
    counts: dict[str, int] = {}

    with engine.begin() as conn:
        if reset:
            for name in DELETE_ORDER:
                conn.execute(tables[name].delete())

        by_movie = in_set("sk_movie_id", selected)

        def load(csv_name: str, **kwargs: Any) -> None:
            table_name = CSV_TO_TABLE[csv_name]
            print(f"Carregando {csv_name}...")
            counts[table_name] = load_table(conn, files[csv_name], tables[table_name], **kwargs)

        load("dim_movies", keep=by_movie)
        load("dim_genres")  # poucas linhas; entram todos os gêneros
        load("bridge_movie_genre", keep=by_movie)
        load("bridge_movie_person", keep=by_movie, collect=collector("sk_person_id", person_ids))
        load("bridge_movie_company", keep=by_movie, collect=collector("sk_company_id", company_ids))
        # Com filtro, só entram pessoas e produtoras que aparecem nas bridges mantidas.
        only_people = person_ids if selected is not None else None
        only_companies = company_ids if selected is not None else None
        load("dim_people", keep=in_set("sk_person_id", only_people))
        load("dim_companies", keep=in_set("sk_company_id", only_companies))
        load("fact_movies_performance", keep=by_movie)
        load("movies_reviews", keep=by_movie)

        violations = conn.exec_driver_sql("PRAGMA foreign_key_check").fetchall()
        if violations:
            raise SeedError(
                f"{len(violations)} violações de chave estrangeira; carga desfeita. "
                f"Primeira: {tuple(violations[0])}"
            )

    engine.dispose()
    raw = sqlite3.connect(db_file)
    try:
        raw.execute("VACUUM")
    finally:
        raw.close()

    print("\nLinhas por tabela:")
    for table_name, total in counts.items():
        print(f"  {table_name}: {total}")
    print(f"Tamanho do arquivo: {db_file.stat().st_size / 1_000_000:.1f} MB")


def positive_int(value: str) -> int:
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("deve ser um inteiro maior que zero")
    return number


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Carrega os CSVs do RocketLab no banco.")
    parser.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR)
    parser.add_argument(
        "--limit", type=positive_int, help="carrega só os N filmes mais votados (padrão: todos)"
    )
    parser.add_argument("--reset", action="store_true", help="apaga os dados antes de carregar")
    args = parser.parse_args(argv)

    try:
        run(args.data_dir, args.limit, args.reset)
    except SeedError as error:
        print(f"Erro: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
