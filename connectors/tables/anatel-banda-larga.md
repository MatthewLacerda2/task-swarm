# Anatel: fixed broadband subscribers per provider and city, 2026

How many fixed broadband connections (acessos) each internet provider has in each
municipality, as providers report to Anatel, the telecom regulator. January to September 2026.

- Source: https://www.anatel.gov.br/dadosabertos/paineis_de_dados/acessos/acessos_banda_larga_fixa.zip
  (1.07 GB, file dated 2026-11-04; downloaded 2026-11-22). It also holds 2013 to 2025, not converted.
- Use that URL as the `source` and 2026-11-04 as the date.
- Made by `connectors/convert.py` in the task-swarm repository.

## Files

| File | Rows | One row is |
|---|---|---|
| `prestadoras_2026.parquet` | 12,429 | One provider (a CNPJ), at the last month of 2026 it reported |
| `acessos_municipio_2026.parquet` | 810,758 | One provider in one municipality in one month |

`cnpj` joins to `cnpj` in `../cnpj-brasil-2026-11/estabelecimentos_*.parquet` (all match;
main activities are mostly `6110803` and `6190601`).

### `prestadoras_2026.parquet`

| Column | Meaning |
|---|---|
| `cnpj`, `empresa`, `grupo_economico` | The provider. `grupo_economico` is `OUTROS` for almost all small ones |
| `porte_prestadora` | Anatel's size class: `Grande Porte` (8 providers) or `Pequeno Porte` |
| `mes_referencia` | The month (1 to 9) the figures are from: 9 for 10,054 providers, earlier for 2,375 |
| `acessos` | Connections in that month |
| `acessos_empresas` | Of those, connections sold to companies (Pessoa Jurídica) |
| `acessos_fibra` | Of those, connections by optical fibre |
| `municipios`, `ufs` | In how many municipalities and states it has connections |
| `uf_principal`, `municipio_principal` | Where it has the most |
| `acessos_2026_01` | Its connections in January 2026, to measure growth. Empty for 2,320 that did not report in January |

### `acessos_municipio_2026.parquet`

`ano`, `mes`, `cnpj`, `empresa`, `grupo_economico`, `porte_prestadora`, `uf`, `municipio`,
`municipio_ibge` (IBGE's 7-digit code), `acessos`, `acessos_empresas`, `acessos_fibra`
(the last two are empty where there are none).

## What to know before trusting a number from it

- Brazil total: 55.8 M connections in January 2026, 58.2 M in September among those who
  reported that month. *Counted from the file on 2026-11-22.*
- **The last months are incomplete for small providers**, who report late: 10,962
  providers reported June, 10,054 reported September. A fall in a small provider's
  numbers near the end of the series is usually a missing report, not lost customers.
  Compare months both of which the provider reported.
- The figures are self-declared. 3,832 providers have 1,000 connections or more, 464
  have 10,000 or more.
- Not kept from the raw file: speed bands, technology in detail, type of product.
