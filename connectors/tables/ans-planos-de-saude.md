# ANS: health-plan operators with members and revenue, 2025 and 2026

Every active health-plan and dental-plan operator in Brazil, from ANS, the regulator:
who it is, how many members (beneficiários) it has in each municipality, and its
accounting statements, which operators must file every quarter. **Revenue here is
reported, not estimated.**

- Sources, all under https://dadosabertos.ans.gov.br/FTP/PDA/ (downloaded 2026-11-22):
  `operadoras_de_plano_de_saude_ativas/Relatorio_cadop.csv` (dated 2026-11-19),
  `informacoes_consolidadas_de_beneficiarios-024/202609/` (September 2026, published 2026-11-05),
  `demonstracoes_contabeis/2025/` and `/2026/` (six quarters, 1T2025 to 2T2026).
- Use the folder's URL as the `source`, with the date of the table used.
- Made by `connectors/convert.py` in the task-swarm repository.
- **Left out on purpose:** street lines, phones, e-mails and the name of each operator's representative.

## Files

| File | Rows | One row is |
|---|---|---|
| `operadoras_resumo.parquet` | 1,101 | One active operator, with members and the main accounting figures. **Start here** |
| `operadoras_2026-11.parquet` | 1,101 | One active operator, register fields only |
| `beneficiarios_2026-09.parquet` | 438,187 | One operator in one municipality, by kind of cover and kind of contract, September 2026 |
| `contas_1T2025.parquet` … `contas_2T2026.parquet` | 0.70 to 0.93 M each | One account of one operator's statement in one quarter |

Operators are keyed by `registro_ans` (6 characters) in every file. `cnpj` joins to
`cnpj` in `../cnpj-brasil-2026-11/estabelecimentos_*.parquet` (all 1,101 match).

### `operadoras_resumo.parquet`

| Column | Meaning |
|---|---|
| `registro_ans`, `cnpj`, `razao_social`, `nome_fantasia` | The operator |
| `modalidade` | Kind: Cooperativa Médica 262, Medicina de Grupo 251, Administradora de Benefícios 181, Autogestão 141, Odontologia de Grupo 138, Cooperativa odontológica 88, Filantropia 33, Seguradora Especializada em Saúde 7 |
| `municipio`, `uf`, `cep` | Head office |
| `regiao_de_comercializacao`, `data_registro_ans` | Where it sells, ANS's code: `1` national or three or more large states, `2` São Paulo state or several states, `3` one state, `4` one of six large capitals, `5` a group of municipalities, `6` one municipality (504 operators are `5`); and the date registered |
| `beneficiarios_medico_2026_09` | Members with medical cover, September 2026. Empty for 436 operators (dental only, or benefit administrators) |
| `beneficiarios_odonto_2026_09` | Members with dental-only cover |
| `municipios_com_beneficiarios` | In how many municipalities it has members |
| `receita_2025` | Total revenue in 2025, in reais (account `3`, RECEITAS). Empty for 104 operators with no 4T2025 statement |
| `contraprestacoes_2025` | Of that, plan premiums net (account `311`) |
| `despesa_2025`, `eventos_assistenciais_2025` | Total expenses (account `4`) and care costs paid (account `41`) |
| `ativo_2025_12`, `patrimonio_liquido_2025_12` | Assets (account `1`) and equity (account `25`) on 2025-12-31 |
| `receita_2026_1sem` | Revenue from January to June 2026 (account `3` in 2T2026) |

### `beneficiarios_2026-09.parquet`

`mes`, `registro_ans`, `cnpj`, `uf`, `municipio_ibge6` (IBGE's code without the last
digit), `municipio`, `cobertura` (`Médico-hospitalar` or `Odontológico`), `contratacao`
(individual, company or association contract), `beneficiarios`, `aderidos_no_mes`,
`cancelados_no_mes`. The municipality is where the member lives.

### `contas_*.parquet`

`trimestre_inicio` (date), `registro_ans`, `conta` (account code: each extra digit is a
level down, `3` > `31` > `311`), `descricao`, `saldo_inicial`, `saldo_final` (reais).

## What to know before trusting a number from it

- **Revenue and expense accounts (`3`, `4`) accumulate through the year** and start again
  in January: `saldo_final` of the fourth quarter is the whole year, and a single
  quarter's revenue is `saldo_final` minus `saldo_inicial`. Checked on one operator across
  3T2025, 4T2025 and 2T2026: each quarter starts where the last ended. Asset and liability
  accounts (`1`, `2`) are balances on the last day.
- Totals: 53.1 M medical members and 37.4 M dental-only in September 2026; R$ 388.2
  billion in 2025 revenue across the 997 active operators with statements, R$ 341.2
  billion of it premiums. *Counted on 2026-11-22.* Largest by revenue: Bradesco Saúde
  (R$ 43.3 bn), Sul América (36.4), Amil (31.7).
- The beneficiaries file has 887 operators, the resumo only the 1,101 active on
  2026-11-19; a few members belong to operators since cancelled.
- Statements are the operators' own filings, as ANS publishes them, not audited here.
