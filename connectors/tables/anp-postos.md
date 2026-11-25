# ANP: every authorised fuel station in Brazil, November 2026

One row per fuel station (posto revendedor) authorised by ANP, the oil and fuel regulator.

- Source: https://www.gov.br/anp/pt-br/centrais-de-conteudo/dados-abertos/dados-cadastrais-dos-revendedores-varejistas-de-combustiveis-automotivos
  (one CSV, replaced by ANP as it updates; downloaded 2026-11-22, newest authorisation in it 2026-11-18).
- Use that URL as the `source` and 2026-11-22 as the date.
- Made by `connectors/convert.py` in the task-swarm repository.
- **Left out on purpose:** the street line and complement. Bairro, CEP and city are kept.

## `postos_2026-11.parquet` — 45,846 rows

| Column | Meaning |
|---|---|
| `cnpj` | The station's CNPJ (14 characters, unique here). Joins to `cnpj` in `../cnpj-brasil-2026-11/estabelecimentos_*.parquet` |
| `razao_social` | Company name |
| `codigo_simp`, `autorizacao` | ANP's own codes for the station |
| `data_autorizacao` | Date (type date) ANP published the authorisation; 2000-08-18 to 2026-11-18, one empty |
| `bairro`, `cep`, `municipio`, `uf` | Where it is. `municipio` is a name in capitals without accents, not a code |
| `bandeira` | The distributor whose brand it carries. `BANDEIRA BRANCA` = unbranded (21,954); then VIBRA 6,961, IPIRANGA 5,638, RAIZEN 4,799, ALE 1,085 |
| `data_vinculacao` | Date it took its current brand |

## What to know before trusting a number from it

- Every CNPJ here is in the CNPJ register (45,846 of 45,846; 45,803 active there). *Checked 2026-11-22.*
- The register has 60,835 active establishments whose main activity is fuel retail
  (`4731800`). The 15,000 or so that are not here have a CNPJ but no ANP authorisation:
  this file is the list of who may sell, the register is not.
- To count a chain's stations, group by the first 8 characters of `cnpj`. The largest:
  SIM Rede de Postos 219, Comercial Buffon 115, Carrefour 102.
- No volumes, prices or number of pumps. ANP publishes weekly prices per station in a
  separate file (Série Histórica de Preços), not downloaded.
