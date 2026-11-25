# Cadastur: registered lodging (hotels, pousadas, hostels) with rooms and beds, June 2026

One row per lodging establishment registered in Cadastur, the Ministry of Tourism's
register of tourism businesses, with its number of rooms and beds as declared by the owner.

- Source: https://dados.turismo.gov.br/dataset/meios-de-hospedagem (resource "Segundo
  Trimestre de 2026", an XLSX published 2026-07-02; downloaded 2026-11-22). Earlier
  quarters back to 2020 are on the same page, not downloaded.
- Use that URL as the `source` and 2026-06-30 as the date.
- Made by `connectors/convert.py` in the task-swarm repository.
- **Left out on purpose:** names of responsible persons, CPFs, phones, e-mails and street
  addresses. The CEP of the commercial address is kept.

## `hospedagem_2026-06.parquet` — 21,109 rows

| Column | Meaning |
|---|---|
| `cnpj` | The establishment's CNPJ (unique here). Joins to `cnpj` in `../cnpj-brasil-2026-11/estabelecimentos_*.parquet` |
| `razao_social`, `nome_fantasia` | Names |
| `tipo_hospedagem` | Hotel 9,570; Pousada 7,587; Outros 1,338; Flat/Apart-Hotel 746; Albergue/Hostel 652; and smaller kinds |
| `unidades_habitacionais` | Rooms (integer). Median: 39 for a hotel, 11 for a pousada |
| `leitos` | Beds (integer); empty in 65 rows |
| `uhs_acessiveis`, `leitos_acessiveis` | Accessible rooms and beds |
| `situacao_cadastral`, `situacao_atividade` | 20,626 are `Regular` / `Operação`; 483 are still being set up |
| `matriz_ou_filial`, `natureza_juridica`, `porte` | As in Receita's register |
| `uf`, `municipio`, `cep_comercial` | Where it is. `cep_comercial` is empty in 9,433 rows |
| `data_abertura` | Date the CNPJ was opened |
| `cnaes` | Activity codes tied to the registration |
| `possui_empregado` | `Sim` / `Não`, declared |
| `website`, `idiomas` | Declared site and languages spoken |
| `validade_certificado` | Date the Cadastur certificate expires |

## What to know before trusting a number from it

- **It is far from every hotel.** Registration is required by law but many do not do it,
  and certificates expire. The CNPJ register has 42,892 active establishments with main
  activity hotel (`5510801`); 14,119 of those are here, about one in three. Totals here
  (791,342 rooms, 1,785,726 beds) are totals of the registered, not of Brazil.
  *Counted on 2026-11-22.*
- Rooms and beds are declared by the owner and not audited.
- Every CNPJ here is in the CNPJ register (20,355 of 21,109 active there).
