"""
Testes sintéticos do coletor_eua (sem rede). Rodar: python testes/teste_coletor.py
Fatos montados no mesmo formato do companyfacts da SEC, com valores "verdadeiros" conhecidos.
Casos cobertos:
  A) exercício fiscal terminando em setembro (estilo Apple); troca de tag de receita entre exercícios;
     10-K/A posterior reapresentando o anual; fluxo de caixa só em YTD; exercício em andamento (só 1T);
     ações de capa (dei); data da 1ª divulgação (point-in-time).
  B) exercício terminando em janeiro (estilo Walmart); 10-Q do 2T ausente; ações só no balanço (multiclasse).
  C) D&A em tag alternativa com cobertura trimestral vs. tag só anual (estilo Microsoft).
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import pandas as pd                                                       # noqa: E402

from coletor_eua import derivar, processar_empresa                     # noqa: E402

M = 1e6


def f(ini, fim, val, form, filed):
    d = {"end": fim, "val": val * M, "form": form, "filed": filed, "accn": "x", "fy": 0, "fp": ""}
    if ini:
        d["start"] = ini
    return d


def add(facts, tag, fato, unidade="USD"):
    facts["facts"]["us-gaap"].setdefault(tag, {"units": {unidade: []}})["units"][unidade].append(fato)


def empresa_a():
    fx = {"facts": {"us-gaap": {}, "dei": {}}}
    fy = {
        2024: dict(S="2023-10-01", ends=["2023-12-30", "2024-03-30", "2024-06-29", "2024-09-28"],
                   rec=[100, 90, 85, 95], ebit=[40, 30, 28, 35], fco=[30, 20, 25, 35], da=[5, 5, 5, 5],
                   q_filed=["2024-02-01", "2024-05-01", "2024-08-01"], k_filed="2024-11-01", tag="Revenues"),
        2025: dict(S="2024-09-29", ends=["2024-12-28", "2025-03-29", "2025-06-28", "2025-09-27"],
                   rec=[110, 95, 90, 105], ebit=[45, 33, 30, 40], fco=[35, 22, 27, 40], da=[6, 6, 6, 6],
                   q_filed=["2025-01-30", "2025-05-01", "2025-07-31"], k_filed="2025-10-31",
                   tag="RevenueFromContractWithCustomerExcludingAssessedTax"),
    }
    for ano, d in fy.items():
        S, e = d["S"], d["ends"]
        ini = [S] + [None] * 3
        import datetime as dt
        for k in range(1, 4):
            ini[k] = (dt.date.fromisoformat(e[k - 1]) + dt.timedelta(days=1)).isoformat()
        for k in range(3):                                    # 10-Qs
            fd = d["q_filed"][k]
            add(fx, d["tag"], f(ini[k], e[k], d["rec"][k], "10-Q", fd))
            add(fx, "OperatingIncomeLoss", f(ini[k], e[k], d["ebit"][k], "10-Q", fd))
            if k:
                add(fx, d["tag"], f(S, e[k], sum(d["rec"][:k + 1]), "10-Q", fd))
                add(fx, "OperatingIncomeLoss", f(S, e[k], sum(d["ebit"][:k + 1]), "10-Q", fd))
            add(fx, "NetCashProvidedByUsedInOperatingActivities", f(S, e[k], sum(d["fco"][:k + 1]), "10-Q", fd))
            add(fx, "DepreciationDepletionAndAmortization", f(S, e[k], sum(d["da"][:k + 1]), "10-Q", fd))
        kf = d["k_filed"]                                      # 10-K
        add(fx, d["tag"], f(S, e[3], sum(d["rec"]), "10-K", kf))
        add(fx, "OperatingIncomeLoss", f(S, e[3], sum(d["ebit"]), "10-K", kf))
        add(fx, "NetCashProvidedByUsedInOperatingActivities", f(S, e[3], sum(d["fco"]), "10-K", kf))
        add(fx, "DepreciationDepletionAndAmortization", f(S, e[3], sum(d["da"]), "10-K", kf))
        for k in range(4):                                     # balanço
            fd = d["q_filed"][k] if k < 3 else kf
            frm = "10-Q" if k < 3 else "10-K"
            add(fx, "Assets", f(None, e[k], 1000 + 10 * k, frm, fd))
            add(fx, "LongTermDebt", f(None, e[k], 200, frm, fd))
            add(fx, "CommercialPaper", f(None, e[k], 20, frm, fd))
            add(fx, "CashAndCashEquivalentsAtCarryingValue", f(None, e[k], 50, frm, fd))
            add(fx, "OperatingLeaseLiability", f(None, e[k], 30, frm, fd))
    # comparativos do FY2024 sob a tag nova (só 3M), publicados nos 10-Qs do FY2025 + anual comparativo no 10-K FY2025
    e24, S24 = fy[2024]["ends"], fy[2024]["S"]
    ini24 = [S24, "2023-12-31", "2024-03-31", "2024-06-30"]
    for k in range(3):
        add(fx, fy[2025]["tag"], f(ini24[k], e24[k], fy[2024]["rec"][k], "10-Q", fy[2025]["q_filed"][k]))
    add(fx, fy[2025]["tag"], f(S24, e24[3], 370, "10-K", fy[2025]["k_filed"]))
    # 10-K/A posterior reapresenta o anual FY2024 da tag antiga: 370 -> 371
    add(fx, "Revenues", f(S24, e24[3], 371, "10-K/A", "2025-12-01"))
    # exercício em andamento FY2026: só 1T
    add(fx, fy[2025]["tag"], f("2025-09-28", "2025-12-27", 120, "10-Q", "2026-01-30"))
    add(fx, "NetCashProvidedByUsedInOperatingActivities", f("2025-09-28", "2025-12-27", 50, "10-Q", "2026-01-30"))
    add(fx, "Assets", f(None, "2025-12-27", 1100, "10-Q", "2026-01-30"))
    fx["facts"]["dei"]["EntityCommonStockSharesOutstanding"] = {"units": {"shares": [
        {"end": "2025-10-17", "val": 15_000 * M, "form": "10-K", "filed": "2025-10-31"},
        {"end": "2026-01-20", "val": 14_900 * M, "form": "10-Q", "filed": "2026-01-30"}]}}
    return fx


def empresa_b():
    fx = {"facts": {"us-gaap": {}, "dei": {}}}
    T = "Revenues"
    # FY2025: 2024-02-01 -> 2025-01-31; 10-Q do 2T ausente
    S, e = "2024-02-01", ["2024-04-30", "2024-07-31", "2024-10-31", "2025-01-31"]
    add(fx, T, f(S, e[0], 50, "10-Q", "2024-06-01"))
    add(fx, "NetCashProvidedByUsedInOperatingActivities", f(S, e[0], 10, "10-Q", "2024-06-01"))
    add(fx, T, f("2024-08-01", e[2], 60, "10-Q", "2024-12-01"))
    add(fx, T, f(S, e[2], 165, "10-Q", "2024-12-01"))
    add(fx, "NetCashProvidedByUsedInOperatingActivities", f(S, e[2], 45, "10-Q", "2024-12-01"))
    add(fx, T, f(S, e[3], 235, "10-K", "2025-03-20"))
    add(fx, "NetCashProvidedByUsedInOperatingActivities", f(S, e[3], 70, "10-K", "2025-03-20"))
    add(fx, "CommonStockSharesOutstanding", f(None, e[3], 8000, "10-K", "2025-03-20"), unidade="shares")
    add(fx, "CommonStockSharesOutstanding", f(None, e[2], 8050, "10-Q", "2024-12-01"), unidade="shares")
    return fx


def empresa_c():
    """Estilo Microsoft: 'Depreciation' só no anual (nota) e 'DepreciationAmortizationAndOther' no fluxo com YTD."""
    fx = {"facts": {"us-gaap": {}, "dei": {}}}
    S, e = "2024-07-01", ["2024-09-30", "2024-12-31", "2025-03-31", "2025-06-30"]
    q = ["2024-10-30", "2025-01-29", "2025-04-30"]
    for k in range(3):
        add(fx, "DepreciationAmortizationAndOther", f(S, e[k], 8 * (k + 1), "10-Q", q[k]))
        add(fx, "OperatingIncomeLoss", f(S, e[k], 30 * (k + 1), "10-Q", q[k]))
    add(fx, "DepreciationAmortizationAndOther", f(S, e[3], 34, "10-K", "2025-07-30"))
    add(fx, "Depreciation", f(S, e[3], 28, "10-K", "2025-07-30"))
    add(fx, "OperatingIncomeLoss", f(S, e[3], 128, "10-K", "2025-07-30"))
    return fx


def checar(nome, obtido, esperado):
    ok = (pd.isna(obtido) and esperado is None) or (esperado is not None and abs(obtido - esperado) < 1e-9)
    print(f"  {'OK ' if ok else 'ERRO'} {nome}: obtido={obtido} esperado={esperado}")
    return ok


def checar_txt(nome, obtido, esperado):
    ok = obtido == esperado
    print(f"  {'OK ' if ok else 'ERRO'} {nome}: obtido={obtido} esperado={esperado}")
    return ok


def rodar():
    tudo = True
    print("Empresa A")
    df, ac, aud = processar_empresa(empresa_a())
    d = derivar(df.copy())
    g = lambda tipo, per, col: d.loc[(d.tipo == tipo) & (d.periodo == per), col].iloc[0]
    tudo &= checar("anual 2024 receita (10-K/A vence)", g("anual", "2024", "receita"), 371)
    tudo &= checar("4T24 receita = 371 - 275", g("trimestral", "4T24", "receita"), 96)
    tudo &= checar("2T24 receita (3M reportado)", g("trimestral", "2T24", "receita"), 90)
    tudo &= checar("anual 2025 receita (tag nova)", g("anual", "2025", "receita"), 400)
    tudo &= checar("4T25 receita", g("trimestral", "4T25", "receita"), 105)
    tudo &= checar("3T25 FCO (YTD diferenciado)", g("trimestral", "3T25", "fco"), 27)
    tudo &= checar("4T25 FCO (anual - 9M)", g("trimestral", "4T25", "fco"), 40)
    tudo &= checar("4T25 EBITDA = 40 + 6", g("trimestral", "4T25", "ebitda"), 46)
    tudo &= checar("LTM 3T25 receita = 96 (4T24 reapresentado)+110+95+90", g("ltm", "LTM 3T25", "receita"), 391)
    tudo &= checar("LTM 1T26 receita = 95+90+105+120", g("ltm", "LTM 1T26", "receita"), 410)
    tudo &= checar("1T26 (exercício em andamento) receita", g("trimestral", "1T26", "receita"), 120)
    tudo &= checar("dívida 4T25 = LongTermDebt + CP", g("trimestral", "4T25", "emprestimos_financiamentos"), 220)
    tudo &= checar("dív. líquida c/ arrend. 4T25 = 220-50+30", g("trimestral", "4T25", "div_liq_com_arrend"), 200)
    tudo &= checar("exercícios fechados", aud["exercicios"], 2)
    tudo &= checar("ações capa mais recente (mi)", ac.sort_values("data")["acoes"].iloc[-1], 14_900)
    arq = lambda tipo, per: g(tipo, per, "dt_arquivamento")
    tudo &= checar_txt("1T24 divulgado em (não o comparativo de 2025)", arq("trimestral", "1T24"), "2024-02-01")
    tudo &= checar_txt("anual 2024 divulgado em (não o 10-K/A)", arq("anual", "2024"), "2024-11-01")
    tudo &= checar_txt("LTM 1T26 divulgado em", arq("ltm", "LTM 1T26"), "2026-01-30")
    print("  fonte 4T24:", g("trimestral", "4T24", "fonte"), "| tags:", aud["tags"][:120])

    print("Empresa B")
    df, ac, aud = processar_empresa(empresa_b())
    d = derivar(df.copy())
    g = lambda tipo, per, col: (d.loc[(d.tipo == tipo) & (d.periodo == per), col].iloc[0]
                                if ((d.tipo == tipo) & (d.periodo == per)).any() else None)
    tudo &= checar("anual 2025 receita", g("anual", "2025", "receita"), 235)
    tudo &= checar("1T25 receita", g("trimestral", "1T25", "receita"), 50)
    tudo &= checar("2T25 ausente (sem 10-Q)", g("trimestral", "2T25", "receita"), None)
    tudo &= checar("3T25 receita (3M reportado)", g("trimestral", "3T25", "receita"), 60)
    tudo &= checar("4T25 receita = 235 - 165", g("trimestral", "4T25", "receita"), 70)
    tudo &= checar("4T25 FCO = 70 - 45", g("trimestral", "4T25", "fco"), 25)
    tudo &= checar("3T25 FCO indeterminado (falta 6M)", g("trimestral", "3T25", "fco"), None)
    tudo &= checar("sem LTM (trimestre faltante)", float((d.tipo == "ltm").sum()), 0.0)
    tudo &= checar("ações = balanço (sem capa)", ac.sort_values("data")["acoes"].iloc[-1], 8000)

    print("Empresa C")
    df, ac, aud = processar_empresa(empresa_c())
    d = derivar(df.copy())
    g = lambda tipo, per, col: d.loc[(d.tipo == tipo) & (d.periodo == per), col].iloc[0]
    tudo &= checar("anual 2025 D&A usa fluxo de caixa (34), não nota (28)", g("anual", "2025", "da"), 34)
    tudo &= checar("4T25 D&A = 34 - 24", g("trimestral", "4T25", "da"), 10)
    print("\nRESULTADO:", "TODOS OK" if tudo else "HÁ ERROS")
    return tudo


if __name__ == "__main__":
    sys.exit(0 if rodar() else 1)
