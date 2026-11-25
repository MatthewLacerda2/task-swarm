#!/bin/bash
# ANS consolidated beneficiaries, one zip per state, for one month (default 2026_09). Resumable.
M=${1:-2026_09}; D=${REGULATORS_DIR:-./regulators}/raw/ans-beneficiarios
mkdir -p $D
UA="Mozilla/5.0 (X11; Linux x86_64) data-fetch"
for uf in AC AL AM AP BA CE DF ES GO MA MG MS MT PA PB PE PI PR RJ RN RO RR RS SC SE SP TO XX; do
  f=pda-024-icb-$uf-$M.zip
  [ -f $D/$f.ok ] && continue
  curl -L -A "$UA" -C - --retry 20 --retry-delay 10 --retry-all-errors -sS -o $D/$f \
    "https://dadosabertos.ans.gov.br/FTP/PDA/informacoes_consolidadas_de_beneficiarios-024/${M/_/}/$f" && unzip -tq $D/$f >/dev/null && touch $D/$f.ok
done
touch $D/.done
