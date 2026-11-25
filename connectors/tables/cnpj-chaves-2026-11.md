# CNPJ register, key columns only: the file to join against

A slim copy of `../cnpj-brasil-2026-11/` made so that nobody has to read the full register
(74 million rows, 20 columns) just to check a list of CNPJs. **If you need to know whether
CNPJs exist, are active, or what activity they declare, use this folder and do not build
your own copy.**

- Made on 2026-11-24 from `../cnpj-brasil-2026-11/estabelecimentos_*.parquet` (Receita
  Federal, release of 2026-11-16) by `connectors/build_chaves.py` in the task-swarm repository.
  Same rows, fewer columns: nothing was filtered out.
- For the source and date of anything taken from here, cite the register as its README says.

| File | Rows | One row is | Size |
|---|---|---|---|
| `estabelecimentos_chaves.parquet` | 74,373,417 | One establishment (a CNPJ of 14 characters), open or closed | 930 MB |
| `empresas_chaves.parquet` | 71,068,585 | One company (`cnpj_basico`, the first 8 characters) | 474 MB |
| `ativos_por_cnae_uf.parquet` | 31,455 | One activity code in one state: how many active establishments and companies | 0.2 MB |

All columns are text except the counts.

- `estabelecimentos_chaves.parquet`: `cnpj`, `cnpj_basico`, `matriz_filial` (`1` head
  office, `2` branch), `situacao_cadastral` (`02` = active; the other codes are in the
  register's README), `cnae_principal` (7 digits), `uf`, `municipio` (Receita's code, not
  IBGE's: names are in `../cnpj-brasil-2026-11/municipios.parquet`).
- `empresas_chaves.parquet`: `cnpj_basico`, `estabelecimentos`, `estabelecimentos_ativos`
  (integers), `cnae_principal_matriz`, `uf_matriz` (of the head office).
- `ativos_por_cnae_uf.parquet`: `cnae_principal`, `uf`, `estabelecimentos_ativos`,
  `empresas_ativas` (integers). Sum over `uf` for Brazil. "How many active establishments
  does the register have in activity X" is answered here in a second.

How to join without loading it all: put your own table of CNPJs in DuckDB and join it to
`read_parquet('.../estabelecimentos_chaves.parquet')`; DuckDB reads the big file as a
stream and keeps only your table in memory. A left join from your table keeps the CNPJs
that are not in the register, which is one of the things to count.

For anything else (names, dates, postcode, secondary activities, company size, Simples)
go to the full register.
