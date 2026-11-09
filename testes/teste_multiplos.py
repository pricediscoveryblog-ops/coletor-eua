"""
Testes do multiplos_eua (sem rede): yfinance falso, base e ações sintéticas.
Casos: desfazer o ajuste de desdobramento do Yahoo; ações corrigidas pelo split entre o dado e a data do preço;
ponto no tempo (só documento já arquivado); LTM preferido ao anual na mesma data; múltiplos e EV.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import numpy as np                                   # noqa: E402
import pandas as pd                                  # noqa: E402

import multiplos_eua as m                            # noqa: E402

D = pd.Timestamp


def yf_falso(lote, ini, fim):
    """Formato do yf.download (campo, ticker). AAA: preço negociado 100 até 14/06/2020, split 2:1 em 15/06/2020,
    depois 50; o Yahoo mostra 50 também antes do split (ajustado). BBB: 20 sempre."""
    idx = pd.bdate_range(ini, fim)
    cols, dados = [], {}
    for t in lote:
        if t == "AAA":
            close = pd.Series(50.0, index=idx)
            spl = pd.Series(0.0, index=idx)
            if D("2020-06-15") in idx:
                spl[D("2020-06-15")] = 2.0
        elif t == "BBB":
            close, spl = pd.Series(20.0, index=idx), pd.Series(0.0, index=idx)
        else:
            continue                                          # ticker sem dado no Yahoo
        dados[("Close", t)] = close
        dados[("Stock Splits", t)] = spl
    df = pd.DataFrame(dados, index=idx)
    df.index.name = "Date"
    df.columns = pd.MultiIndex.from_tuples(df.columns)
    return df


def checar(nome, obtido, esperado, tol=1e-6):
    ok = (pd.isna(obtido) and esperado is None) or (esperado is not None and pd.notna(obtido)
                                                   and abs(obtido - esperado) <= tol * max(1, abs(esperado)))
    print(f"  {'OK ' if ok else 'ERRO'} {nome}: obtido={obtido} esperado={esperado}")
    return ok


def rodar():
    tudo = True
    # ---- preços
    pr, sp, falhas = m.baixar(["AAA", "BBB", "ZZZ"], D("2020-01-01"), D("2020-12-31"), yf_falso)
    g = lambda t, d: pr[(pr.ticker == t) & (pr.data == D(d))]["bruto"].iloc[0]
    tudo &= checar("AAA antes do split = preço negociado 100", g("AAA", "2020-03-31"), 100.0)
    tudo &= checar("AAA no dia do split = 50", g("AAA", "2020-06-15"), 50.0)
    tudo &= checar("AAA depois = 50", g("AAA", "2020-09-30"), 50.0)
    tudo &= checar("ticker sem dado vira falha", float("ZZZ" in falhas), 1.0)
    tudo &= checar("split registrado", float(len(sp)), 1.0)

    # ---- base: empresa 1 (AAA) com 10-Q do 1T20 e 2T20; empresa 2 (BBB) só anual
    linhas = []
    def L(cik, tipo, per, fim, arq, **v):
        r = dict(cik=cik, cnpj=f"CIK{cik:011d}", ticker="?", empresa=f"E{cik}", tipo=tipo, periodo=per, dt_fim=D(fim),
                 dt_arquivamento=D(arq), receita=1000.0, ebitda=300.0, ebitda_pre_ifrs16=280.0,
                 ebitda_ifrs16_equiv=np.nan, custo_arrend_operacional=np.nan, lucro_controladores=200.0,
                 dividendos_pagos=50.0, pl_controladores=4000.0, div_liq_com_arrend=500.0, div_liq_sem_arrend=400.0,
                 nao_controladores=10.0, passivo_arrend_operacional=100.0)
        r.update(v)
        linhas.append(r)
    L(1, "anual", "2019", "2019-12-31", "2020-02-20", custo_arrend_operacional=20.0, ebitda_ifrs16_equiv=320.0)
    L(1, "ltm", "LTM 4T19", "2019-12-31", "2020-02-20", ebitda_ifrs16_equiv=320.0, lucro_controladores=200.0)
    L(1, "trimestral", "4T19", "2019-12-31", "2020-02-20")
    L(1, "trimestral", "1T20", "2020-03-31", "2020-05-05")
    L(1, "ltm", "LTM 1T20", "2020-03-31", "2020-05-05", lucro_controladores=250.0)
    L(1, "trimestral", "2T20", "2020-06-30", "2020-08-05")
    L(1, "ltm", "LTM 2T20", "2020-06-30", "2020-08-05", lucro_controladores=300.0)
    L(2, "anual", "2019", "2019-12-31", "2020-03-01", passivo_arrend_operacional=0.0)
    base = pd.DataFrame(linhas)
    cad = pd.DataFrame({"cik": [1, 2], "ticker": ["AAA", "BBB"], "tickers": ["AAA", "BBB|BBB-PA"]})
    acoes = pd.DataFrame({"cik": [1, 1, 2], "data": [D("2020-01-25"), D("2020-08-01"), D("2020-02-15")],
                          "acoes": [1000.0, 2000.0, 500.0], "fonte_acoes": ["capa (dei)"] * 3})
    alv = pd.DataFrame({"ticker": ["AAA"] * 4 + ["BBB"] * 2,
                        "alvo": [D("2019-12-31"), D("2020-03-31"), D("2020-06-30"), D("2020-12-31"), D("2019-12-31"),
                                 D("2020-12-31")]})          # main() acrescenta o último pregão para todos
    pr2, sp2, _ = m.baixar(["AAA", "BBB"], D("2019-12-01"), D("2020-12-31"), yf_falso)
    pa = m.resolver_alvos(alv, pr2)
    ult = D("2020-12-31")
    mult = m.calcular(base, cad, acoes, pa, sp2, ult)
    q = lambda c, tipo, d, col: mult[(mult.cik == c) & (mult.tipo == tipo) & (mult.data == D(d))][col].iloc[0]

    # 31/03/2020: ponto no tempo -> LTM 4T19 (o 1T20 só sai em 05/05); preço 100 x 1000 ações = 100.000
    tudo &= checar("1T20: documento = LTM 4T19 (ponto no tempo)", float(q(1, "trimestral", "2020-03-31", "periodo_base") == "LTM 4T19"), 1.0)
    tudo &= checar("1T20: valor de mercado 100 x 1.000 mi", q(1, "trimestral", "2020-03-31", "valor_mercado"), 100000.0)
    tudo &= checar("1T20: P/L = 100.000 / 200", q(1, "trimestral", "2020-03-31", "p_l"), 500.0)
    # 30/06/2020: depois do split; último dado de ações (25/01, 1.000) x 2 (split) = 2.000; preço 50 -> 100.000
    tudo &= checar("2T20: ações corrigidas pelo split (2.000 mi)", q(1, "trimestral", "2020-06-30", "acoes_on"), 2000e6)
    tudo &= checar("2T20: valor de mercado estável no split", q(1, "trimestral", "2020-06-30", "valor_mercado"), 100000.0)
    tudo &= checar("2T20: documento = LTM 1T20", float(q(1, "trimestral", "2020-06-30", "periodo_base") == "LTM 1T20"), 1.0)
    # EV e EV/EBITDA (EBITDA equivalente: LTM 1T20 sem o dado -> EBITDA 300 + custo do último 10-K 20 = 320)
    tudo &= checar("2T20: EV c/ arrend = 100.000 + 500 + 10", q(1, "trimestral", "2020-06-30", "ev_com_arrend"), 100510.0)
    tudo &= checar("2T20: EV/EBITDA equiv = 100.510 / 320", q(1, "trimestral", "2020-06-30", "ev_ebitda_ifrs16"), 100510.0 / 320)
    tudo &= checar("2T20: EV/EBITDA após arrend. fin. = 100.410 / 280", q(1, "trimestral", "2020-06-30", "ev_ebitda_pre_ifrs16"), 100410.0 / 280)
    tudo &= checar("2T20: P/VPA = 100.000 / 4.000", q(1, "trimestral", "2020-06-30", "p_vpa"), 25.0)
    tudo &= checar("2T20: dividend yield = 50 / 100.000", q(1, "trimestral", "2020-06-30", "dividend_yield"), 0.0005)
    # anual 2019: números do exercício, preço 31/12/2019 (100) x ações até então (nenhuma antes de 25/01 -> vazio)
    tudo &= checar("anual 2019 AAA sem ações até a data -> vazio", q(1, "anual", "2019-12-31", "valor_mercado") if
                   len(mult[(mult.cik == 1) & (mult.tipo == "anual")]) else np.nan, None)
    # 31/12/2020 (último pregão): LTM 2T20, ações 2.000 (dado de 01/08, já pós-split), preço 50
    tudo &= checar("último pregão: documento LTM 2T20", float(q(1, "trimestral", "2020-12-31", "periodo_base") == "LTM 2T20"), 1.0)
    tudo &= checar("último pregão: valor de mercado 50 x 2.000", q(1, "trimestral", "2020-12-31", "valor_mercado"), 100000.0)
    # BBB: só anual; no último pregão usa o 10-K (nota) e marca multiclasse; sem arrendamento operacional
    tudo &= checar("BBB último pregão: valor 20 x 500", q(2, "trimestral", "2020-12-31", "valor_mercado"), 10000.0)
    nota = q(2, "trimestral", "2020-12-31", "notas")
    tudo &= checar("BBB nota 'sem LTM' e 'mais de um ticker'", float("sem LTM" in nota and "mais de um ticker" in nota), 1.0)
    tudo &= checar("BBB EV/EBITDA equiv = EBITDA (sem arrend. operacional)", q(2, "trimestral", "2020-12-31", "ev_ebitda_ifrs16"), 10510.0 / 300)
    # cache de splits vazio (como na 1ª execução) concatenado com splits novos: razão chega como 'object'
    vazio = pd.DataFrame(columns=["ticker", "data", "razao"])
    juntos = pd.concat([vazio, sp2], ignore_index=True)
    isp = m.indice_splits(juntos)
    tudo &= checar("splits após concat com cache vazio (tipo object)", float(isp["idx"].iloc[0]), 2.0)
    mult2 = m.calcular(base, cad, acoes, pa, juntos, ult)
    tudo &= checar("cálculo completo com splits 'object'", q(1, "trimestral", "2020-06-30", "valor_mercado"), 100000.0)
    # caso real da 1ª carga: preços com tipo genérico ('object') e um preço zero (ticker sem negócio)
    pa_obj = pa.copy()
    pa_obj["preco_bruto"] = pa_obj["preco_bruto"].astype(object)
    pa_obj.loc[pa_obj.index[0], "preco_bruto"] = 0.0
    try:
        mult3 = m.calcular(base, cad, acoes.astype({"acoes": object}), pa_obj, juntos, ult)
        ok3 = True
    except Exception as e:
        print("   ", type(e).__name__, e)
        ok3 = False
    tudo &= checar("preço 'object' com zero não derruba o cálculo", float(ok3), 1.0)
    tudo &= checar("valor de mercado numérico (float)", float(mult3["valor_mercado"].dtype.kind == "f") if ok3 else 0.0, 1.0)
    # caches vazios nascem tipados
    vazio_cache = m._ler("nao_existe.parquet", ["ticker", "alvo", "data_preco", "preco_bruto"])
    tudo &= checar("cache vazio com preço numérico", float(vazio_cache["preco_bruto"].dtype.kind == "f"), 1.0)
    print("\nRESULTADO:", "TODOS OK" if tudo else "HÁ ERROS")
    return tudo


if __name__ == "__main__":
    sys.exit(0 if rodar() else 1)
