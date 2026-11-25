# Connectors

Scripts that turn files published by Brazilian regulators into Parquet tables with one
row per company or establishment, keyed by CNPJ. The tables go into a folder that a
run receives with `--data`, so agents start from official figures (beds of a hospital,
subscribers of an internet provider, rooms of a hotel) and not from a web search.

Only the code and the description of each table are here. The downloaded files and the
tables stay out of the repository.

| Script | What it does |
|---|---|
| `download_big.sh` | Anatel's fixed-broadband zip (about 1 GB) and the CNES zip (about 750 MB). Resumable |
| `download_ans_benef.sh` | ANS beneficiaries, one zip per state, for a month given as `2026_09`. Resumable |
| `convert.py` | Raw files to Parquet, one step per source: `anp`, `anatel`, `cadastur`, `ans`, `cnes`. A file that exists is skipped, and a file only gets its final name once it is whole, so it is safe to run again after an interruption |
| `build_chaves.py` | A slim copy of the CNPJ register (key columns only) that makes the joins fast |

The small files (ANP's CSV, Cadastur's XLSX, ANS's operator list and statements) are
fetched with one `curl` each; the address is in each table's description.

## Tables

| Table | Source | One row is | Measures |
|---|---|---|---|
| [`anp-postos`](tables/anp-postos.md) | ANP | a fuel station | none: the brand it carries and when it was authorised |
| [`anatel-banda-larga`](tables/anatel-banda-larga.md) | Anatel | an internet provider; a provider in a city | subscribers, by technology |
| [`cadastur-hospedagem`](tables/cadastur-hospedagem.md) | Ministry of Tourism | a lodging establishment | rooms and beds |
| [`ans-planos-de-saude`](tables/ans-planos-de-saude.md) | ANS | a health-plan operator | members, revenue, expenses |
| [`cnes-saude`](tables/cnes-saude.md) | DATASUS | a health establishment | beds, equipment, staff |
| [`cnpj-chaves-2026-11`](tables/cnpj-chaves-2026-11.md) | Receita Federal | an establishment; a company | none: keys for joining |

Each description gives the source address, the date of the file, the columns, what was
checked against the CNPJ register and what to know before trusting a number from it.

## Running

    export REGULATORS_DIR=/path/to/regulators   # gets raw/ and tmp/
    export LIBRARY_DATA=/path/to/library/data   # gets one folder per table
    ./download_big.sh
    ./download_ans_benef.sh 2026_09
    uv run --with duckdb --with openpyxl --with pyarrow python -I convert.py
    uv run --with duckdb python -I build_chaves.py

`build_chaves.py` reads a Parquet copy of the CNPJ register at
`$LIBRARY_DATA/cnpj-brasil-2026-11/`, which these scripts do not make.

File names carry the month of the data. To add a month, download it and change the
names in `convert.py`.

## Personal data

The raw files hold phone numbers, e-mail addresses, street lines, names of responsible
persons and some CPFs. `convert.py` leaves all of those out of every table, and keeps
only establishments that are legal entities where the source mixes them with
individuals. Keep `raw/` out of any folder handed to a run.
