"""
Múltiplos de mercado do universo americano — Price Discovery
Preços: Yahoo Finance via yfinance (gratuito, sem chave). Fundamentos: base SEC do coletor (dados/eua_*.parquet).

Saída: dados/multiplos_eua.parquet, no MESMO formato do multiplos.csv.gz da base brasileira:
  tipo "anual"      -> um ponto no fim de cada exercício, com os números do próprio exercício
  tipo "trimestral" -> um ponto no fim de cada trimestre fiscal (ponto no tempo: último documento JÁ DIVULGADO até a
                       data, pela coluna dt_arquivamento; lucro/EBITDA/receita dos 12 meses desse documento) e um ponto
                       no último pregão, sobrescrito a cada execução
Valores em US$ milhões.

Preço x ações na mesma base:
  - o Yahoo devolve preços ajustados por desdobramentos; aqui eles voltam ao preço negociado na data
    (preço_bruto = Close x produto dos desdobramentos posteriores à data);
  - ações (dados/acoes_eua.parquet) = último dado até a data, corrigido pelos desdobramentos entre a data do dado e a
    data do preço.
Empresas com mais de uma classe listada: ações totais do balanço x preço do ticker principal (aproximação, com nota).

Caches (dados/): precos_alvo.parquet (preço por data-alvo), precos_recentes.parquet (últimos ~200 dias),
splits_eua.parquet. A primeira execução baixa o histórico completo (desde 2013-12); as seguintes, só os últimos dias.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import sys
import time
from pathlib import Path

import warnings

import numpy as np
import pandas as pd

warnings.simplefilter("ignore", pd.errors.PerformanceWarning)

DADOS = Path(__file__).resolve().parent / "dados"
INICIO = pd.Timestamp("2013-12-01")
JANELA_RECENTE = 200          # dias guardados no cache de preços recentes
DIAS_DIARIO = 25              # dias baixados na atualização diária
TOL_PRECO = 10                # preço aceito até N dias antes da data-alvo (feriados, ações sem negócio)
LOTE = int(os.environ.get("YF_LOTE", "100"))
MAX_BACKFILL = int(os.environ.get("YF_MAX_BACKFILL", "6000"))

COLS_BASE = ["cik", "cnpj", "ticker", "empresa", "tipo", "periodo", "dt_fim", "dt_arquivamento", "receita",
             "ebitda", "ebitda_pre_ifrs16", "ebitda_ifrs16_equiv", "custo_arrend_operacional", "lucro_controladores",
             "dividendos_pagos", "pl_controladores", "div_liq_com_arrend", "div_liq_sem_arrend", "nao_controladores",
             "passivo_arrend_operacional"]


# =============================================================================== preços (yfinance)
def _para_longo(df: pd.DataFrame, lote: list[str]) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Saída do yf.download -> (preços: ticker, data, close, split) e (splits: ticker, data, razao)."""
    vazio = (pd.DataFrame(columns=["ticker", "data", "close", "split"]), pd.DataFrame(columns=["ticker", "data", "razao"]))
    if df is None or df.empty:
        return vazio
    if not isinstance(df.columns, pd.MultiIndex):                      # versões antigas, um ticker só
        df = df.copy()
        df.columns = pd.MultiIndex.from_product([df.columns, [lote[0]]])
    if "Close" not in set(df.columns.get_level_values(0)):              # layout (ticker, campo)
        df = df.swaplevel(axis=1)
    close = df["Close"].copy()
    spl = df["Stock Splits"].copy() if "Stock Splits" in set(df.columns.get_level_values(0)) else close * 0
    c = close.reset_index().melt(id_vars=close.index.name or "Date", var_name="ticker", value_name="close")
    s = spl.reset_index().melt(id_vars=spl.index.name or "Date", var_name="ticker", value_name="split")
    c.columns = ["data", "ticker", "close"]
    s.columns = ["data", "ticker", "split"]
    longo = c.merge(s, on=["data", "ticker"], how="left").dropna(subset=["close"])
    longo["data"] = pd.to_datetime(longo["data"]).dt.tz_localize(None).dt.normalize()
    longo["split"] = longo["split"].fillna(0.0)
    splits = longo[(longo["split"] > 0) & (longo["split"] != 1)][["ticker", "data", "split"]].rename(
        columns={"split": "razao"})
    return longo, splits


def preco_bruto(longo: pd.DataFrame) -> pd.DataFrame:
    """Close ajustado por desdobramentos -> preço negociado na data (x produto dos desdobramentos POSTERIORES)."""
    if longo.empty:
        return longo.assign(bruto=pd.Series(dtype=float))
    longo = longo.sort_values(["ticker", "data"]).copy()
    r = longo["split"].where(longo["split"] > 0, 1.0)
    # produto acumulado de trás para frente, excluindo a própria data (o Close do dia do split já é pós-split)
    log_r = np.log(r.astype(float))
    depois = longo.assign(_l=log_r).groupby("ticker")["_l"].transform(lambda x: x[::-1].cumsum()[::-1]) - log_r
    longo["bruto"] = longo["close"] * np.exp(depois)
    return longo


def baixar(tickers: list[str], inicio: pd.Timestamp, fim: pd.Timestamp | None = None, baixador=None):
    """Baixa em lotes; devolve (precos_longos com 'bruto', splits). 'baixador' permite injetar um falso nos testes."""
    if baixador is None:
        import yfinance as yf
        def baixador(lote, ini, fim_):
            return yf.download(lote, start=ini.strftime("%Y-%m-%d"),
                               end=(fim_ + pd.Timedelta(days=1)).strftime("%Y-%m-%d") if fim_ is not None else None,
                               interval="1d", auto_adjust=False, actions=True, progress=False, threads=True,
                               group_by="column")
    precos, splits, falhas = [], [], []
    for i in range(0, len(tickers), LOTE):
        lote = tickers[i:i + LOTE]
        df = None
        for tentativa in range(4):
            try:
                df = baixador(lote, inicio, fim)
                break
            except Exception as e:                                        # limite de requisições etc.
                espera = 20 * (tentativa + 1)
                print(f"  yfinance falhou ({type(e).__name__}: {str(e)[:80]}); nova tentativa em {espera}s")
                time.sleep(espera)
        p, s = _para_longo(df, lote)
        falhas += sorted(set(lote) - set(p["ticker"]))
        precos.append(preco_bruto(p))
        splits.append(s)
        if (i // LOTE) % 10 == 0:
            print(f"  preços: {min(i + LOTE, len(tickers))}/{len(tickers)} tickers")
        time.sleep(1.0)
    pr = pd.concat(precos, ignore_index=True) if precos else pd.DataFrame()
    sp = pd.concat(splits, ignore_index=True) if splits else pd.DataFrame(columns=["ticker", "data", "razao"])
    return pr, sp, falhas


# =============================================================================== preço em cada data-alvo
def _norm(df: pd.DataFrame, datas=(), textos=("ticker",), inteiros=("cik",)) -> pd.DataFrame:
    """Mesmos tipos de chave em todos os merges (caches lidos do parquet vêm com tipos diferentes dos recém-criados)."""
    df = df.copy()
    for c in datas:
        if c in df.columns:
            df[c] = pd.to_datetime(df[c]).astype("datetime64[ns]")
    for c in textos:
        if c in df.columns:
            df[c] = df[c].astype(object).where(df[c].notna(), None).astype(object)
    for c in inteiros:
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce").astype("Int64").astype("int64")
    return df


def resolver_alvos(alvos: pd.DataFrame, precos: pd.DataFrame) -> pd.DataFrame:
    """alvos (ticker, alvo) -> + data_preco, preco_bruto: último preço até a data, com até TOL_PRECO dias de atraso."""
    if alvos.empty or precos.empty:
        return alvos.assign(data_preco=pd.NaT, preco_bruto=np.nan)
    a = _norm(alvos, datas=("alvo",)).sort_values("alvo")
    p = _norm(precos[["ticker", "data", "bruto"]].rename(columns={"data": "data_preco", "bruto": "preco_bruto"}),
              datas=("data_preco",)).sort_values("data_preco")
    out = pd.merge_asof(a, p, left_on="alvo", right_on="data_preco", by="ticker", direction="backward",
                        tolerance=pd.Timedelta(days=TOL_PRECO))
    return out


def indice_splits(splits: pd.DataFrame) -> pd.DataFrame:
    """Índice acumulado de desdobramentos por ticker: produto das razões até a data (inclusive)."""
    if splits.empty:
        return pd.DataFrame(columns=["ticker", "data", "idx"])
    s = _norm(splits, datas=("data",)).drop_duplicates(["ticker", "data"]).sort_values(["ticker", "data"])
    s["razao"] = pd.to_numeric(s["razao"], errors="coerce").astype(float)   # concat com cache vazio vira 'object'
    s = s[s["razao"] > 0]
    if s.empty:
        return pd.DataFrame(columns=["ticker", "data", "idx"])
    s["idx"] = s.groupby("ticker")["razao"].cumprod()
    return s[["ticker", "data", "idx"]]


def idx_em(df: pd.DataFrame, col_data: str, isp: pd.DataFrame, nome: str) -> pd.DataFrame:
    if isp.empty:
        return df.assign(**{nome: 1.0})
    df = _norm(df, datas=(col_data,))
    isp = _norm(isp.rename(columns={"data": "_d"}), datas=("_d",))
    m = pd.merge_asof(df.sort_values(col_data), isp.sort_values("_d"),
                      left_on=col_data, right_on="_d", by="ticker", direction="backward")
    return m.drop(columns="_d").rename(columns={"idx": nome}).assign(**{nome: lambda x: x[nome].fillna(1.0)})


# =============================================================================== múltiplos
def calcular(base: pd.DataFrame, cad: pd.DataFrame, acoes: pd.DataFrame, precos_alvo: pd.DataFrame,
             splits: pd.DataFrame, ultimo_pregao: pd.Timestamp) -> pd.DataFrame:
    """Monta os pontos (anual, trimestral, último pregão) e calcula os múltiplos."""
    b = _norm(base, datas=("dt_fim", "dt_arquivamento"))
    cad = _norm(cad)
    acoes = _norm(acoes, datas=("data",))
    precos_alvo = _norm(precos_alvo, datas=("alvo", "data_preco"))
    ultimo_pregao = pd.Timestamp(ultimo_pregao).as_unit("ns") if hasattr(pd.Timestamp(ultimo_pregao), "as_unit") \
        else pd.Timestamp(ultimo_pregao)
    tick = cad.set_index("cik")["ticker"].to_dict()
    tks_all = cad.set_index("cik")["tickers"].to_dict() if "tickers" in cad.columns else {}
    b["ticker"] = b["cik"].map(tick).fillna(b["ticker"])

    # custo de arrendamento operacional do último 10-K divulgado (EBITDA equivalente IFRS 16 no LTM sem o dado)
    anu = b[b["tipo"] == "anual"].sort_values("dt_arquivamento")
    ltm = b[b["tipo"] == "ltm"].sort_values("dt_arquivamento")

    pontos = []
    # anual: números do próprio exercício, preço no fim do exercício
    pa = anu[anu["dt_fim"] >= INICIO].copy()
    pa["data"] = pa["dt_fim"]
    pa["tipo_m"] = "anual"
    pontos.append(pa)
    # trimestral: fim de cada trimestre fiscal + último pregão, com o último LTM já divulgado
    tri_datas = b[b["tipo"] == "trimestral"][["cik", "ticker", "dt_fim"]].drop_duplicates()
    tri_datas = tri_datas[tri_datas["dt_fim"] >= INICIO].rename(columns={"dt_fim": "data"})
    hoje = _norm(cad[["cik", "ticker"]].assign(data=ultimo_pregao), datas=("data",))
    hoje = hoje[hoje["cik"].isin(b["cik"])]
    datas = _norm(pd.concat([tri_datas, hoje], ignore_index=True), datas=("data",)) \
        .drop_duplicates(["cik", "data"]).sort_values("data")
    # mesma data de arquivamento (10-K traz anual e LTM 4T): o LTM fica por último e é o escolhido
    fonte = pd.concat([anu.assign(_pri=0), ltm.assign(_pri=1)]).sort_values(["dt_arquivamento", "_pri"])
    fonte = fonte.dropna(subset=["dt_arquivamento"])
    pt = pd.merge_asof(datas, fonte.drop(columns=["ticker"]), left_on="data", right_on="dt_arquivamento", by="cik",
                       direction="backward")
    pt["tipo_m"] = "trimestral"
    pontos.append(pt)
    p = pd.concat(pontos, ignore_index=True)
    p = p.dropna(subset=["dt_fim"])                                    # sem documento divulgado até a data

    # EBITDA equivalente IFRS 16 nos 12 meses: do próprio documento; senão EBITDA + custo de arrend. operacional do
    # último 10-K; sem arrendamento operacional: = EBITDA
    custo = pd.merge_asof(p[["cik", "data"]].reset_index().sort_values("data"),
                          anu[["cik", "dt_arquivamento", "custo_arrend_operacional"]].dropna()
                          .rename(columns={"custo_arrend_operacional": "_custo"}).sort_values("dt_arquivamento"),
                          left_on="data", right_on="dt_arquivamento", by="cik", direction="backward") \
        .set_index("index")["_custo"]
    p["_custo_ref"] = custo.reindex(p.index)
    eq = p["ebitda_ifrs16_equiv"]
    eq = eq.where(eq.notna(), p["ebitda"] + p["_custo_ref"])
    sem_op = p["passivo_arrend_operacional"].fillna(0) == 0
    p["ebitda_equiv_12m"] = eq.where(eq.notna() | ~sem_op, p["ebitda"])

    # preço
    p = p.merge(precos_alvo.rename(columns={"alvo": "data"}), on=["ticker", "data"], how="left")
    # ações: último dado até a data do preço (ou da data-alvo), corrigido pelos desdobramentos no intervalo
    ac = acoes[["cik", "data", "acoes", "fonte_acoes"]].rename(columns={"data": "data_acoes"}).copy()
    ac["data_acoes"] = pd.to_datetime(ac["data_acoes"])
    p["_ref"] = p["data_preco"].fillna(p["data"])
    p = pd.merge_asof(p.sort_values("_ref"), ac.sort_values("data_acoes"), left_on="_ref", right_on="data_acoes",
                      by="cik", direction="backward")
    isp = indice_splits(splits)
    p = idx_em(p, "_ref", isp, "_idx_preco")
    p = idx_em(p.assign(_da=p["data_acoes"].fillna(p["_ref"])), "_da", isp, "_idx_acoes")
    p["acoes_ajust"] = p["acoes"] * p["_idx_preco"] / p["_idx_acoes"]

    pos = lambda s: s.where(s > 0)
    p["valor_mercado"] = p["preco_bruto"] * p["acoes_ajust"]          # US$ x milhões de ações = US$ milhões
    nc = p["nao_controladores"].fillna(0)
    p["ev_com_arrend"] = p["valor_mercado"] + p["div_liq_com_arrend"] + nc
    p["ev_sem_arrend"] = p["valor_mercado"] + p["div_liq_sem_arrend"] + nc
    p["p_vpa"] = p["valor_mercado"] / pos(p["pl_controladores"])
    p["p_l"] = p["valor_mercado"] / pos(p["lucro_controladores"])
    p["ev_ebitda_ifrs16"] = p["ev_com_arrend"] / pos(p["ebitda_equiv_12m"])
    p["ev_ebitda_pre_ifrs16"] = p["ev_sem_arrend"] / pos(p["ebitda_pre_ifrs16"])
    p["ev_receita_com_arrend"] = p["ev_com_arrend"] / pos(p["receita"])
    p["ev_receita_sem_arrend"] = p["ev_sem_arrend"] / pos(p["receita"])
    p["dividend_yield"] = p["dividendos_pagos"].fillna(0) / pos(p["valor_mercado"])

    # notas
    n = pd.Series("", index=p.index)
    n[p["preco_bruto"].isna()] += "sem cotação até 10 dias antes da data; "
    n[p["acoes"].isna()] += "sem quantidade de ações; "
    n[(p["_idx_preco"] != p["_idx_acoes"]) & p["acoes"].notna()] += "ações corrigidas por desdobramento; "
    n[p["fonte_acoes"].fillna("").str.contains("multiclasse")] += "capa parcial: ações do balanço; "
    multi = p["cik"].map(lambda c: "|" in str(tks_all.get(c, "")))
    n[multi] += "mais de um ticker listado: ações totais x preço do ticker principal; "
    n[(p["tipo_m"] == "trimestral") & (p["tipo"] == "anual")] += "sem LTM: números do último 10-K; "
    p["notas"] = n.str.strip("; ")

    out = pd.DataFrame({
        "cik": p["cik"], "cnpj": p["cnpj"], "ticker": p["ticker"], "empresa": p["empresa"], "tipo": p["tipo_m"],
        "data": p["data"], "periodo_base": p["periodo"], "dt_fim_base": p["dt_fim"],
        "dt_arquivamento_base": p["dt_arquivamento"],
        "valor_mercado": p["valor_mercado"], "ev_com_arrend": p["ev_com_arrend"], "ev_sem_arrend": p["ev_sem_arrend"],
        "ticker_on": p["ticker"], "preco_on": p["preco_bruto"], "data_preco_on": p["data_preco"],
        "acoes_on": p["acoes_ajust"] * 1e6, "ticker_pn": None, "preco_pn": np.nan, "data_preco_pn": pd.NaT,
        "acoes_pn": np.nan,
        "p_vpa": p["p_vpa"], "p_l": p["p_l"], "ev_ebitda_ifrs16": p["ev_ebitda_ifrs16"],
        "ev_ebitda_pre_ifrs16": p["ev_ebitda_pre_ifrs16"], "ev_receita_com_arrend": p["ev_receita_com_arrend"],
        "ev_receita_sem_arrend": p["ev_receita_sem_arrend"], "dividend_yield": p["dividend_yield"],
        "dividendos_12m": p["dividendos_pagos"], "pl_controladores": p["pl_controladores"],
        "lucro_controladores_12m": p["lucro_controladores"], "ebitda_12m": p["ebitda_equiv_12m"],
        "ebitda_pre_ifrs16_12m": p["ebitda_pre_ifrs16"], "receita_12m": p["receita"], "notas": p["notas"]})
    out = out[out["valor_mercado"].notna() | (out["data"] == ultimo_pregao)]
    # alerta de salto: valor de mercado 5x maior/menor que o ponto anterior sem variação equivalente de preço
    out = out.sort_values(["cik", "tipo", "data"])
    g = out.groupby(["cik", "tipo"])
    rv = out["valor_mercado"] / g["valor_mercado"].shift(1)
    rp = out["preco_on"] / g["preco_on"].shift(1)
    salto = ((rv > 5) | (rv < 0.2)) & (rp.between(0.5, 2))
    out.loc[salto, "notas"] = (out.loc[salto, "notas"] + "; possível erro de ações (salto)").str.strip("; ")
    return out.reset_index(drop=True)


# =============================================================================== execução
def carregar_base() -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    partes = []
    for t in ("anual", "trimestral", "ltm"):
        x = pd.read_parquet(DADOS / f"eua_{t}.parquet")
        partes.append(x[[c for c in COLS_BASE if c in x.columns]])
    base = pd.concat(partes, ignore_index=True)
    for c in COLS_BASE:
        if c not in base.columns:
            base[c] = np.nan
    cad = pd.read_parquet(DADOS / "cadastro_eua.parquet")
    acoes = pd.read_parquet(DADOS / "acoes_eua.parquet")
    return base, cad, acoes


def _ler(nome, colunas):
    arq = DADOS / nome
    return pd.read_parquet(arq) if arq.exists() else pd.DataFrame(columns=colunas)


def main(baixador=None, hoje: pd.Timestamp | None = None):
    hoje = (hoje or pd.Timestamp.today()).normalize()
    base, cad, acoes = carregar_base()
    cad = cad[cad["cik"].isin(base["cik"].unique())]
    tickers = sorted(cad["ticker"].dropna().unique())
    print(f"múltiplos: {len(tickers)} tickers")

    cache = _ler("precos_alvo.parquet", ["ticker", "alvo", "data_preco", "preco_bruto"])
    recentes = _ler("precos_recentes.parquet", ["ticker", "data", "bruto"])
    splits = _ler("splits_eua.parquet", ["ticker", "data", "razao"])
    for df_, c in ((cache, "alvo"), (cache, "data_preco"), (recentes, "data"), (splits, "data")):
        df_[c] = pd.to_datetime(df_[c])

    # datas-alvo: fins de exercício e de trimestre fiscal desde 2013-12 + hoje
    tk = cad.set_index("cik")["ticker"]
    alv = base[base["tipo"].isin(["anual", "trimestral"])][["cik", "dt_fim"]].drop_duplicates()
    alv["ticker"] = alv["cik"].map(tk)
    alv = alv.dropna(subset=["ticker"]).rename(columns={"dt_fim": "alvo"})
    alv["alvo"] = pd.to_datetime(alv["alvo"])
    alv = alv[alv["alvo"] >= INICIO][["ticker", "alvo"]].drop_duplicates()

    # 1) histórico completo para tickers sem cache (primeira execução, IPOs, empresas novas)
    com_cache = set(cache["ticker"].unique()) | set(recentes["ticker"].unique())
    novos = [t for t in tickers if t not in com_cache][:MAX_BACKFILL]
    falhas = []
    if novos:
        print(f"histórico completo: {len(novos)} tickers desde {INICIO.date()}")
        pr, sp, f1 = baixar(novos, INICIO, hoje, baixador)
        falhas += f1
        if not pr.empty:
            cache = pd.concat([cache, resolver_alvos(alv[alv["ticker"].isin(novos)], pr)], ignore_index=True)
            recentes = pd.concat([recentes, pr[pr["data"] >= hoje - pd.Timedelta(days=JANELA_RECENTE)]
                                  [["ticker", "data", "bruto"]]], ignore_index=True)
        splits = pd.concat([splits, sp], ignore_index=True)
    # 2) últimos dias para os demais
    velhos = [t for t in tickers if t not in novos]
    if velhos:
        print(f"atualização: {len(velhos)} tickers, últimos {DIAS_DIARIO} dias")
        pr, sp, f2 = baixar(velhos, hoje - pd.Timedelta(days=DIAS_DIARIO), hoje, baixador)
        falhas += f2
        if not pr.empty:
            recentes = pd.concat([recentes, pr[["ticker", "data", "bruto"]]], ignore_index=True)
        splits = pd.concat([splits, sp], ignore_index=True)
    recentes = recentes.drop_duplicates(["ticker", "data"], keep="last")
    recentes = recentes[recentes["data"] >= hoje - pd.Timedelta(days=JANELA_RECENTE)]
    splits = splits.drop_duplicates(["ticker", "data"], keep="last")

    # 3) datas-alvo ainda sem preço que caem na janela recente
    cache = cache.dropna(subset=["preco_bruto"]).drop_duplicates(["ticker", "alvo"], keep="last")
    falta = alv.merge(cache[["ticker", "alvo"]], on=["ticker", "alvo"], how="left", indicator=True)
    falta = falta[falta["_merge"] == "left_only"].drop(columns="_merge")
    falta = falta[falta["alvo"] >= hoje - pd.Timedelta(days=JANELA_RECENTE - TOL_PRECO)]
    if len(falta):
        cache = pd.concat([cache, resolver_alvos(falta, recentes).dropna(subset=["preco_bruto"])], ignore_index=True)

    # 4) último pregão
    ultimo_pregao = recentes["data"].max() if len(recentes) else hoje
    hoje_alvo = pd.DataFrame({"ticker": tickers, "alvo": ultimo_pregao})
    precos_alvo = pd.concat([cache, resolver_alvos(hoje_alvo, recentes)], ignore_index=True) \
        .drop_duplicates(["ticker", "alvo"], keep="last")

    # caches gravados antes do cálculo: se o cálculo falhar, o download não se perde
    DADOS.mkdir(exist_ok=True)
    splits["razao"] = pd.to_numeric(splits["razao"], errors="coerce")
    cache.to_parquet(DADOS / "precos_alvo.parquet", index=False, compression="zstd")
    recentes.to_parquet(DADOS / "precos_recentes.parquet", index=False, compression="zstd")
    splits.to_parquet(DADOS / "splits_eua.parquet", index=False, compression="zstd")
    print(f"caches gravados: {len(cache)} preços em datas-alvo, {len(recentes)} recentes, {len(splits)} desdobramentos",
          flush=True)

    mult = calcular(base, cad, acoes, precos_alvo, splits, ultimo_pregao)
    mult.to_parquet(DADOS / "multiplos_eua.parquet", index=False, compression="zstd")
    ult = mult[(mult["tipo"] == "trimestral") & (mult["data"] == ultimo_pregao)]
    resumo = {"gerado_em": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
              "ultimo_pregao": str(ultimo_pregao.date()), "tickers": len(tickers),
              "com_valor_mercado_hoje": int(ult["valor_mercado"].notna().sum()),
              "linhas": {t: int((mult["tipo"] == t).sum()) for t in ("anual", "trimestral")},
              "falhas_download": len(set(falhas)), "exemplo_falhas": sorted(set(falhas))[:30],
              "fonte_precos": "Yahoo Finance (yfinance)"}
    (DADOS / "multiplos_meta.json").write_text(json.dumps(resumo, ensure_ascii=False, indent=2))
    print(json.dumps(resumo, ensure_ascii=False, indent=2))
    conferencia(mult, ultimo_pregao)
    return mult


def conferencia(mult, ultimo_pregao, tickers=("AAPL", "MSFT", "GOOGL", "WMT", "KO", "DAL", "NVDA")):
    u = mult[(mult["tipo"] == "trimestral") & (mult["data"] == ultimo_pregao) & mult["ticker"].isin(tickers)]
    cols = ["ticker", "data_preco_on", "preco_on", "acoes_on", "valor_mercado", "p_l", "p_vpa", "ev_ebitda_ifrs16",
            "dividend_yield", "periodo_base", "notas"]
    with pd.option_context("display.width", 250, "display.max_columns", 20, "display.max_colwidth", 60):
        print("\n=== conferência múltiplos (US$ milhões; ações em unidades) ===")
        num = [c for c in cols if pd.api.types.is_float_dtype(u[c])]
        print(u[cols].round({c: 2 for c in num}).to_string(index=False))


if __name__ == "__main__":
    main()
