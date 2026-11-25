"""A slim copy of the CNPJ register for joins: one row per establishment, key columns only.

    uv run --with duckdb python -I build_chaves.py

Reads the library's data/cnpj-brasil-2026-11/ and writes data/cnpj-chaves-2026-11/ next to
it. Skips files that exist. LIBRARY_DATA is the library's data folder (default
./library/data); REGULATORS_DIR holds tmp/ for DuckDB's scratch files (default ./regulators).
"""
import os
from pathlib import Path
import duckdb

L = Path(os.environ.get("LIBRARY_DATA", "library/data")).resolve()
TMP = Path(os.environ.get("REGULATORS_DIR", "regulators")).resolve() / "tmp"
OUT = L / "cnpj-chaves-2026-11"
con = duckdb.connect()
con.execute("set memory_limit='6GB'")
con.execute(f"set temp_directory='{TMP}/duckdb'")
con.execute("set preserve_insertion_order=false")


def write(sql, out):
    if out.exists():
        print("skip", out.name); return
    part = out.with_suffix(".parquet.part"); part.unlink(missing_ok=True)
    con.execute(f"copy ({sql}) to '{part}' (format parquet, compression zstd)")
    part.rename(out)
    print(out.name, con.execute(f"select count(*) from '{out}'").fetchall()[0][0])


src = f"'{L}/cnpj-brasil-2026-11/estabelecimentos_*.parquet'"
print(con.execute(f"describe select * from {src}").fetchall())
write(f"select cnpj, cnpj_basico, matriz_filial, situacao_cadastral, cnae_principal, uf, municipio from {src}",
      OUT / "estabelecimentos_chaves.parquet")
k = f"'{OUT}/estabelecimentos_chaves.parquet'"
write(f"""select cnae_principal, uf, count(*) as estabelecimentos_ativos,
                 count(distinct cnpj_basico) as empresas_ativas
          from {k} where situacao_cadastral = '02' group by all""",
      OUT / "ativos_por_cnae_uf.parquet")
write(f"""select cnpj_basico, count(*) as estabelecimentos,
                 count(*) filter (situacao_cadastral = '02') as estabelecimentos_ativos,
                 any_value(cnae_principal) filter (matriz_filial = '1') as cnae_principal_matriz,
                 any_value(uf) filter (matriz_filial = '1') as uf_matriz
          from {k} group by 1""",
      OUT / "empresas_chaves.parquet")
