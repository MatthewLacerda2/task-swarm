#!/bin/bash
# Resumable downloads of the two large regulator files. Safe to run again after a power cut.
R=${REGULATORS_DIR:-./regulators}
mkdir -p $R/raw/anatel-banda-larga $R/raw/cnes
UA="Mozilla/5.0 (X11; Linux x86_64) data-fetch"
curl -L -A "$UA" -C - --retry 20 --retry-delay 15 --retry-all-errors -sS \
  -o $R/raw/anatel-banda-larga/acessos_banda_larga_fixa.zip \
  "https://www.anatel.gov.br/dadosabertos/paineis_de_dados/acessos/acessos_banda_larga_fixa.zip" && touch $R/raw/anatel-banda-larga/.done
curl -C - --retry 20 --retry-delay 15 --retry-all-errors -sS \
  -o $R/raw/cnes/BASE_DE_DADOS_CNES_202610.ZIP \
  "ftp://ftp.datasus.gov.br/cnes/BASE_DE_DADOS_CNES_202610.ZIP" && touch $R/raw/cnes/.done
