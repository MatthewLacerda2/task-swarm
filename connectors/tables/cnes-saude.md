# CNES: every health establishment in Brazil with beds, equipment and staff, October 2026

The Ministry of Health's national register of health establishments (CNES): hospitals,
clinics, laboratories, pharmacies, health posts, public and private. One row per
establishment, with its beds, its equipment and how many professionals work there.

- Source: ftp://ftp.datasus.gov.br/cnes/BASE_DE_DADOS_CNES_202610.ZIP (748 MB, published
  2026-11-15; downloaded 2026-11-22). A new file comes out every month.
- Use that URL as the `source` and October 2026 as the date.
- Made by `connectors/convert.py` in the task-swarm repository.
- **Left out on purpose:** the 171,390 establishments registered to a person and not a
  company (mostly a doctor's or dentist's own office), and for the rest street lines,
  phones, e-mails, CPFs and every row about a named professional. Staff are counts only.

## Files

| File | Rows | One row is |
|---|---|---|
| `estabelecimentos_2026-10.parquet` | 472,204 | One establishment, with totals of beds, equipment and staff. **Start here** |
| `leitos_2026-10.parquet` | 60,995 | One kind of bed in one establishment |
| `equipamentos_2026-10.parquet` | 1,078,114 | One kind of equipment in one establishment |

The three join on `co_unidade` (or `cnes`, the 7-digit public code).

### `estabelecimentos_2026-10.parquet`

| Column | Meaning |
|---|---|
| `co_unidade`, `cnes` | The establishment's keys |
| `cnpj` | Its own CNPJ. Empty for 120,669 that belong to a keeper, e.g. a city hall's health posts |
| `cnpj_mantenedora` | The keeper's CNPJ, where there is one |
| `cnpj_proprio_ou_mantenedora` | `cnpj` if there is one, else the keeper's. Joins to `cnpj` in `../cnpj-brasil-2026-11/estabelecimentos_*.parquet` |
| `razao_social`, `nome_fantasia` | Names |
| `bairro`, `cep`, `municipio`, `uf`, `municipio_ibge6` | Where it is (`municipio_ibge6`: IBGE's code without the last digit) |
| `latitude`, `longitude` | Numbers; present in 439,235 rows |
| `tipo_unidade`, `co_tipo_unidade` | Kind: CONSULTORIO ISOLADO, CLINICA/CENTRO DE ESPECIALIDADE, FARMACIA, HOSPITAL GERAL (5,573 in operation with beds, 443,147 beds), HOSPITAL ESPECIALIZADO, … |
| `natureza_juridica`, `co_natureza_juridica` | Legal nature. First digit of the code: `1` public administration, `2` business, `3` non-profit |
| `gestao` | Who manages it in the public system: `M` city (447,917), `E` state, `D` both, `S` none |
| `motivo_desabilitacao` | **Not empty = the establishment is deactivated** (84,669 rows). Filter `motivo_desabilitacao is null` for the 387,535 in operation |
| `leitos`, `leitos_sus` | Beds in all, and beds available to the public system (SUS) |
| `equipamentos`, `equipamentos_imagem` | Pieces of equipment in all, and imaging equipment (X-ray, ultrasound, CT, MRI) |
| `profissionais`, `medicos`, `enfermeiros` | Distinct professionals tied to the establishment; of those, physicians (occupation code 225…) and nurses (2235…) |
| `site`, `atualizado_em` | Declared site; date the record was last updated |

`leitos_2026-10.parquet`: `co_unidade`, `cnes`, `co_leito`, `leito` (speciality),
`tipo_leito`, `leitos_existentes`, `leitos_sus`.
`equipamentos_2026-10.parquet`: `co_unidade`, `cnes`, `co_equipamento`, `equipamento`,
`tipo_equipamento`, `existentes`, `em_uso`.

## What to know before trusting a number from it

- In operation: 543,759 beds, of which 212,650 in public establishments, 138,787 in
  businesses and 192,322 in non-profits. *Counted on 2026-11-22.*
- **A professional who works in three places is counted in each.** Summing
  `profissionais` over establishments (6.49 M) counts jobs, not people.
- A company with several establishments has several rows; group by
  `cnpj_proprio_ou_mantenedora`, or by its first 8 characters for the whole company.
- 352,976 of the 352,988 distinct CNPJs are in the CNPJ register; 293,011 are active there.
- Records are kept by each establishment and its city's health office; some are years
  out of date (see `atualizado_em`).
- Not converted: services offered, teams, working hours per professional.
