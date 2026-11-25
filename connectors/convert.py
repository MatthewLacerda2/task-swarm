"""Turn the regulators' raw files into Parquet in the task-swarm library.

    uv run --with duckdb --with openpyxl --with pyarrow python -I convert.py [anp anatel cadastur ans cnes]

Two folders, both set in the environment:
    REGULATORS_DIR  holds raw/<source>/ (the downloads, see download_*.sh and README.md)
                    and tmp/ (scratch space). Default: ./regulators
    LIBRARY_DATA    where the Parquet files go, one folder per source. Default: ./library/data

Safe to run again after a power cut: a Parquet file that exists is skipped, and a
file only gets its final name once it is complete.

Left out of every table on purpose, as in the library's CNPJ copy: phone numbers, e-mails,
street lines, names of responsible persons and CPFs. The raw files keep them.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import duckdb

BASE = Path(os.environ.get("REGULATORS_DIR", "regulators")).resolve()
RAW = BASE / "raw"
TMP = BASE / "tmp"
LIB = Path(os.environ.get("LIBRARY_DATA", "library/data")).resolve()

con = duckdb.connect()
con.execute("set memory_limit='6GB'")
con.execute(f"set temp_directory='{TMP}/duckdb'")
con.execute("set preserve_insertion_order=false")


def write(sql: str, out: Path) -> None:
    """Run a query into a Parquet file, under a temporary name until it is whole."""
    if out.exists():
        print(f"  skip {out.name} (exists)")
        return
    out.parent.mkdir(parents=True, exist_ok=True)
    part = out.with_suffix(".parquet.part")
    part.unlink(missing_ok=True)
    con.execute(f"copy ({sql}) to '{part}' (format parquet, compression zstd)")
    part.rename(out)
    rows = con.execute(f"select count(*) from '{out}'").fetchall()[0][0]
    print(f"  wrote {out.name}: {rows:,} rows")


def unzip(archive: Path, member: str | None, dest: Path) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    cmd = ["unzip", "-o", "-q", str(archive)] + ([member] if member else []) + ["-d", str(dest)]
    subprocess.run(cmd, check=True)


def csv(path: str, extra: str = "") -> str:
    return f"read_csv('{path}', delim=';', header=true, all_varchar=true, quote='\"'{extra})"


# --- ANP: fuel stations -------------------------------------------------------------------


def anp() -> None:
    src = RAW / "anp-postos/dados-cadastrais-revendedores-varejistas-combustiveis-automoveis.csv"
    write(
        f"""
        select CODIGOISIMP as codigo_simp, AUTORIZACAO as autorizacao,
               try_strptime(DATAPUBLICACAO, '%d/%m/%Y')::date as data_autorizacao,
               RAZAOSOCIAL as razao_social, lpad(CNPJ, 14, '0') as cnpj,
               BAIRRO as bairro, CEP as cep, UF as uf, MUNICIPIO as municipio,
               BANDEIRA as bandeira,
               try_strptime(DATAVINCULACAO, '%d/%m/%Y')::date as data_vinculacao
        from {csv(src)}
        """,
        LIB / "anp-postos/postos_2026-11.parquet",
    )


# --- Anatel: fixed broadband ----------------------------------------------------------------


def providers(out_dir: Path) -> None:
    """One row per provider, at the last month of 2026 it reported (small providers report late)."""
    m = f"'{out_dir / 'acessos_municipio_2026.parquet'}'"
    write(
        f"""
        with last as (select cnpj, max(mes) as mes from {m} group by 1),
        now as (
            select a.cnpj, a.mes as mes_referencia, any_value(empresa) as empresa,
                   any_value(grupo_economico) as grupo_economico,
                   any_value(porte_prestadora) as porte_prestadora,
                   sum(acessos) as acessos, coalesce(sum(acessos_empresas), 0) as acessos_empresas,
                   coalesce(sum(acessos_fibra), 0) as acessos_fibra,
                   count(distinct municipio_ibge) as municipios, count(distinct uf) as ufs,
                   arg_max(uf, acessos) as uf_principal,
                   arg_max(municipio, acessos) as municipio_principal
            from {m} a join last using (cnpj, mes) group by a.cnpj, a.mes
        ),
        jan as (select cnpj, sum(acessos) as acessos_2026_01 from {m} where mes = 1 group by cnpj)
        select 2026 as ano, now.*, jan.acessos_2026_01
        from now left join jan using (cnpj)
        """,
        out_dir / "prestadoras_2026.parquet",
    )


def anatel() -> None:
    out_dir = LIB / "anatel-banda-larga"
    if (out_dir / "acessos_municipio_2026.parquet").exists():
        providers(out_dir)
        return
    member = "Acessos_Banda_Larga_Fixa_2026.csv"
    unzip(RAW / "anatel-banda-larga/acessos_banda_larga_fixa.zip", member, TMP / "anatel")
    src = csv(TMP / "anatel" / member)
    write(
        f"""
        select "Ano"::int as ano, "Mês"::int as mes, lpad("CNPJ", 14, '0') as cnpj,
               any_value("Empresa") as empresa, any_value("Grupo Econômico") as grupo_economico,
               any_value("Porte da Prestadora") as porte_prestadora,
               "UF" as uf, any_value("Município") as municipio,
               "Código IBGE Município" as municipio_ibge,
               sum("Acessos"::bigint) as acessos,
               sum("Acessos"::bigint) filter ("Tipo de Pessoa" = 'Pessoa Jurídica') as acessos_empresas,
               sum("Acessos"::bigint) filter ("Meio de Acesso" = 'Fibra') as acessos_fibra
        from {src}
        group by all
        """,
        out_dir / "acessos_municipio_2026.parquet",
    )
    providers(out_dir)
    (TMP / "anatel" / member).unlink(missing_ok=True)


# --- Cadastur: lodging ----------------------------------------------------------------------


def cadastur() -> None:
    out = LIB / "cadastur-hospedagem/hospedagem_2026-06.parquet"
    if out.exists():
        print(f"  skip {out.name} (exists)")
        return
    import openpyxl
    import pyarrow as pa

    wb = openpyxl.load_workbook(
        RAW / "cadastur-hospedagem/meios-de-hospedagem-2-trimestre-2026.xlsx", read_only=True
    )
    rows = wb.worksheets[0].iter_rows(values_only=True)
    header = [str(h).strip() for h in next(rows)]
    cols: dict[str, list] = {h: [] for h in header}
    for row in rows:
        if row[1] is None:
            continue
        for h, v in zip(header, row):
            cols[h].append(None if v is None else str(v))
    con.register("raw", pa.table(cols))
    write(
        """
        select lpad("Número de Inscrição do CNPJ", 14, '0') as cnpj,
               "Nome da Pessoa Jurídica" as razao_social, "Nome Fantasia" as nome_fantasia,
               "Situação Cadastral" as situacao_cadastral, "Situação da Atividade" as situacao_atividade,
               "Tipo de Estabelecimento" as matriz_ou_filial, "Natureza Jurídica" as natureza_juridica,
               "Porte" as porte, "UF" as uf, "Município" as municipio,
               regexp_extract("Endereço Completo Comercial", 'CEP: (\\d{8})', 1) as cep_comercial,
               try_cast("Data de Abertura" as date) as data_abertura,
               "CNAE(S) relacionados à atividade" as cnaes,
               "Possui Empregado?" as possui_empregado,
               nullif("Website", '-') as website, nullif("Idiomas", '-') as idiomas,
               try_cast("Validade do Certificado" as timestamp)::date as validade_certificado,
               "Tipo de Hospedagem" as tipo_hospedagem,
               try_cast("Unidade Habitacionais" as int) as unidades_habitacionais,
               try_cast("Leitos" as int) as leitos,
               try_cast("UHs Acessíveis" as int) as uhs_acessiveis,
               try_cast("Leitos Acessíveis" as int) as leitos_acessiveis
        from raw
        """,
        out,
    )


# --- ANS: health-plan operators ---------------------------------------------------------------

UFS = "AC AL AM AP BA CE DF ES GO MA MG MS MT PA PB PE PI PR RJ RN RO RR RS SC SE SP TO XX".split()
QUARTERS = ["1T2025", "2T2025", "3T2025", "4T2025", "1T2026", "2T2026"]


def ans() -> None:
    out_dir = LIB / "ans-planos-de-saude"
    write(
        f"""
        select lpad(REGISTRO_OPERADORA, 6, '0') as registro_ans, lpad(CNPJ, 14, '0') as cnpj,
               RAZAO_SOCIAL as razao_social, NOME_FANTASIA as nome_fantasia,
               MODALIDADE as modalidade, CIDADE as municipio, UF as uf, CEP as cep,
               REGIAO_DE_COMERCIALIZACAO as regiao_de_comercializacao,
               try_cast(DATA_REGISTRO_ANS as date) as data_registro_ans
        from {csv(RAW / 'ans-operadoras/Relatorio_cadop.csv')}
        """,
        out_dir / "operadoras_2026-11.parquet",
    )

    # Beneficiaries: one state at a time, so the unzipped CSVs never pile up on disk.
    parts = TMP / "ans-benef-parts"
    for uf in UFS:
        part = parts / f"{uf}.parquet"
        if part.exists():
            continue
        name = f"pda-024-icb-{uf}-2026_09"
        unzip(RAW / f"ans-beneficiarios/{name}.zip", None, TMP / "ans-benef")
        write(
            f"""
            select ID_CMPT_MOVEL as mes, lpad(CD_OPERADORA, 6, '0') as registro_ans,
                   lpad(NR_CNPJ, 14, '0') as cnpj, SG_UF as uf, CD_MUNICIPIO as municipio_ibge6,
                   any_value(NM_MUNICIPIO) as municipio,
                   COBERTURA_ASSIST_PLAN as cobertura, DE_CONTRATACAO_PLANO as contratacao,
                   sum(QT_BENEFICIARIO_ATIVO::bigint) as beneficiarios,
                   sum(QT_BENEFICIARIO_ADERIDO::bigint) as aderidos_no_mes,
                   sum(QT_BENEFICIARIO_CANCELADO::bigint) as cancelados_no_mes
            from {csv(TMP / 'ans-benef' / (name + '.csv'))}
            group by all
            """,
            part,
        )
        (TMP / "ans-benef" / f"{name}.csv").unlink(missing_ok=True)
    write(f"select * from '{parts}/*.parquet'", out_dir / "beneficiarios_2026-09.parquet")

    # Financial statements: every account, six quarters.
    for q in QUARTERS:
        out = out_dir / f"contas_{q}.parquet"
        if out.exists():
            print(f"  skip {out.name} (exists)")
            continue
        unzip(RAW / f"ans-demonstracoes/{q}.zip", None, TMP / "ans-contas")
        write(
            f"""
            select try_cast("DATA" as date) as trimestre_inicio, lpad(REG_ANS, 6, '0') as registro_ans,
                   CD_CONTA_CONTABIL as conta, DESCRICAO as descricao,
                   replace(VL_SALDO_INICIAL, ',', '.')::double as saldo_inicial,
                   replace(VL_SALDO_FINAL, ',', '.')::double as saldo_final
            from {csv(TMP / 'ans-contas' / (q + '.csv'))}
            """,
            out,
        )
        (TMP / "ans-contas" / f"{q}.csv").unlink(missing_ok=True)

    # One row per operator: who it is, how many members, how much it took in.
    def conta(q: str, code: str, name: str) -> str:
        return (
            f"(select registro_ans, sum(saldo_final) as {name} from '{out_dir}/contas_{q}.parquet' "
            f"where conta = '{code}' group by 1) as {name}_t"
        )

    joins = [
        conta("4T2025", "3", "receita_2025"),
        conta("4T2025", "311", "contraprestacoes_2025"),
        conta("4T2025", "4", "despesa_2025"),
        conta("4T2025", "41", "eventos_assistenciais_2025"),
        conta("4T2025", "1", "ativo_2025_12"),
        conta("4T2025", "25", "patrimonio_liquido_2025_12"),
        conta("2T2026", "3", "receita_2026_1sem"),
    ]
    join_sql = " ".join(f"left join {j} using (registro_ans)" for j in joins)
    write(
        f"""
        with b as (
            select registro_ans,
                   sum(beneficiarios) filter (cobertura = 'Médico-hospitalar') as beneficiarios_medico_2026_09,
                   sum(beneficiarios) filter (cobertura = 'Odontológico') as beneficiarios_odonto_2026_09,
                   count(distinct municipio_ibge6) filter (beneficiarios > 0) as municipios_com_beneficiarios
            from '{out_dir}/beneficiarios_2026-09.parquet' group by 1
        )
        select o.*, b.* exclude (registro_ans),
               receita_2025, contraprestacoes_2025, despesa_2025, eventos_assistenciais_2025,
               ativo_2025_12, patrimonio_liquido_2025_12, receita_2026_1sem
        from '{out_dir}/operadoras_2026-11.parquet' o
        left join b using (registro_ans) {join_sql}
        """,
        out_dir / "operadoras_resumo.parquet",
    )


# --- CNES: health establishments --------------------------------------------------------------


def cnes() -> None:
    out_dir = LIB / "cnes-saude"
    month = "202610"
    names = [
        "tbEstabelecimento", "rlEstabComplementar", "rlEstabEquipamento", "tbLeito", "tbEquipamento",
        "tbTipoUnidade", "tbTipoEquipamento", "tbMunicipio", "tbNaturezaJuridica", "tbCargaHorariaSus",
    ]
    done = all((out_dir / f).exists() for f in (
        "estabelecimentos_2026-10.parquet", "leitos_2026-10.parquet", "equipamentos_2026-10.parquet"))
    if done:
        print("  skip cnes (exists)")
        return
    d = TMP / "cnes"
    if not all((d / f"{n}{month}.csv").exists() for n in names):
        d.mkdir(parents=True, exist_ok=True)
        subprocess.run(["unzip", "-o", "-q", str(RAW / f"cnes/BASE_DE_DADOS_CNES_{month}.ZIP")]
                       + [f"{n}{month}.csv" for n in names] + ["-d", str(d)], check=True)

    def t(n: str) -> str:
        return csv(d / f"{n}{month}.csv", ", encoding='latin-1'")

    write(
        f"""
        select c.CO_UNIDADE as co_unidade, e.CO_CNES as cnes, c.CO_LEITO as co_leito,
               trim(l.DS_LEITO) as leito, l.TP_LEITO as tipo_leito,
               c.QT_EXIST::int as leitos_existentes, c.QT_SUS::int as leitos_sus
        from {t('rlEstabComplementar')} c
        join {t('tbEstabelecimento')} e using (CO_UNIDADE)
        left join {t('tbLeito')} l using (CO_LEITO)
        where e.TP_PFPJ = '3'
        """,
        out_dir / "leitos_2026-10.parquet",
    )
    write(
        f"""
        select q.CO_UNIDADE as co_unidade, e.CO_CNES as cnes, q.CO_EQUIPAMENTO as co_equipamento,
               trim(k.DS_EQUIPAMENTO) as equipamento, trim(tk.DS_TIPO_EQUIPAMENTO) as tipo_equipamento,
               q.QT_EXISTENTE::int as existentes, q.QT_USO::int as em_uso
        from {t('rlEstabEquipamento')} q
        join {t('tbEstabelecimento')} e using (CO_UNIDADE)
        left join {t('tbEquipamento')} k
               on k.CO_EQUIPAMENTO = q.CO_EQUIPAMENTO and trim(k.CO_TIPO_EQUIPAMENTO) = trim(q.CO_TIPO_EQUIPAMENTO)
        left join {t('tbTipoEquipamento')} tk on trim(tk.CO_TIPO_EQUIPAMENTO) = trim(q.CO_TIPO_EQUIPAMENTO)
        where e.TP_PFPJ = '3'
        """,
        out_dir / "equipamentos_2026-10.parquet",
    )
    write(
        f"""
        with staff as (
            select CO_UNIDADE, count(distinct CO_PROFISSIONAL_SUS) as profissionais,
                   count(distinct CO_PROFISSIONAL_SUS) filter (CO_CBO like '225%') as medicos,
                   count(distinct CO_PROFISSIONAL_SUS) filter (CO_CBO like '2235%') as enfermeiros
            from {t('tbCargaHorariaSus')} group by 1
        ),
        beds as (select co_unidade as CO_UNIDADE, sum(leitos_existentes) as leitos, sum(leitos_sus) as leitos_sus
                 from '{out_dir}/leitos_2026-10.parquet' group by 1),
        eq as (select co_unidade as CO_UNIDADE, sum(existentes) as equipamentos,
                      sum(existentes) filter (tipo_equipamento = 'DIAGNOSTICO POR IMAGEM') as equipamentos_imagem
               from '{out_dir}/equipamentos_2026-10.parquet' group by 1)
        select e.CO_UNIDADE as co_unidade, e.CO_CNES as cnes,
               nullif(e.NU_CNPJ, '') as cnpj, nullif(e.NU_CNPJ_MANTENEDORA, '') as cnpj_mantenedora,
               coalesce(nullif(e.NU_CNPJ, ''), nullif(e.NU_CNPJ_MANTENEDORA, '')) as cnpj_proprio_ou_mantenedora,
               e.NO_RAZAO_SOCIAL as razao_social, e.NO_FANTASIA as nome_fantasia,
               e.NO_BAIRRO as bairro, e.CO_CEP as cep,
               e.CO_MUNICIPIO_GESTOR as municipio_ibge6, m.NO_MUNICIPIO as municipio, m.CO_SIGLA_ESTADO as uf,
               e.TP_UNIDADE as co_tipo_unidade, trim(tu.DS_TIPO_UNIDADE) as tipo_unidade,
               e.CO_NATUREZA_JUR as co_natureza_juridica, trim(nj.DS_NATUREZA_JUR) as natureza_juridica,
               e.TP_GESTAO as gestao, nullif(e.CO_MOTIVO_DESAB, '') as motivo_desabilitacao,
               try_cast(nullif(e.NU_LATITUDE, '') as double) as latitude,
               try_cast(nullif(e.NU_LONGITUDE, '') as double) as longitude,
               nullif(e.NO_URL, '') as site,
               try_strptime(e."TO_CHAR(DT_ATUALIZACAO,'DD/MM/YYYY')", '%d/%m/%Y')::date as atualizado_em,
               coalesce(beds.leitos, 0) as leitos, coalesce(beds.leitos_sus, 0) as leitos_sus,
               coalesce(eq.equipamentos, 0) as equipamentos,
               coalesce(eq.equipamentos_imagem, 0) as equipamentos_imagem,
               coalesce(staff.profissionais, 0) as profissionais,
               coalesce(staff.medicos, 0) as medicos, coalesce(staff.enfermeiros, 0) as enfermeiros
        from {t('tbEstabelecimento')} e
        left join {t('tbMunicipio')} m on m.CO_MUNICIPIO = e.CO_MUNICIPIO_GESTOR
        left join {t('tbTipoUnidade')} tu on tu.CO_TIPO_UNIDADE = e.TP_UNIDADE
        left join {t('tbNaturezaJuridica')} nj on nj.CO_NATUREZA_JUR = e.CO_NATUREZA_JUR
        left join beds using (CO_UNIDADE) left join eq using (CO_UNIDADE) left join staff using (CO_UNIDADE)
        where e.TP_PFPJ = '3'
        """,
        out_dir / "estabelecimentos_2026-10.parquet",
    )


STEPS = {"anp": anp, "anatel": anatel, "cadastur": cadastur, "ans": ans, "cnes": cnes}

if __name__ == "__main__":
    for name in sys.argv[1:] or list(STEPS):
        print(f"== {name}", flush=True)
        STEPS[name]()
