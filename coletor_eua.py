"""
Coletor do universo americano — Price Discovery
Fonte: SEC EDGAR (XBRL companyfacts + submissions + company_tickers_exchange). Gratuito, sem chave.

Modos
  python coletor_eua.py completo      baixa companyfacts.zip (~1,4 GB) e recalcula todas as empresas listadas
  python coletor_eua.py incremental   só empresas com 10-K/10-Q nos últimos DIAS_INCREMENTAL dias (API por empresa)

Saídas (pasta dados/)
  eua_anual.parquet, eua_trimestral.parquet, eua_ltm.parquet   mesmo esquema/nomes da base BR sempre que o conceito é igual
  acoes_eua.parquet     série de ações em circulação (capa dei / balanço / média) com alerta de salto
  cadastro_eua.parquet  cadastro do universo (ticker, bolsa, SIC, setor/subsetor/segmento)
  auditoria_eua.csv.gz  por empresa: tags usadas, exercícios, lacunas, motivo de exclusão
  metadados.json
Unidades: US$ milhões; ações em milhões.
"""
from __future__ import annotations

import datetime as dt
import io
import json
import os
import re
import sys
import time
import zipfile
from collections import Counter, defaultdict
from pathlib import Path

import numpy as np
import orjson
import pandas as pd
import requests

from conceitos_eua import (ACOES_DURACAO, ACOES_INSTANTE, DEI_ACOES, DURACAO, INSTANTE, setor_de,
                           subsetor_de, tipo_especial, todas_tags)

RAIZ = Path(__file__).resolve().parent
DADOS = RAIZ / "dados"
CACHE = RAIZ / "cache"
UA = os.environ.get("SEC_USER_AGENT", "PriceDiscovery pricediscovery.blog@gmail.com")
BOLSAS = {"NYSE", "Nasdaq", "CBOE", "TXSE"}
URL_TICKERS = "https://www.sec.gov/files/company_tickers_exchange.json"
URL_ZIP = "https://www.sec.gov/Archives/edgar/daily-index/xbrl/companyfacts.zip"
URL_FACTS = "https://data.sec.gov/api/xbrl/companyfacts/CIK{:010d}.json"
URL_SUBM = "https://data.sec.gov/submissions/CIK{:010d}.json"
URL_IDX = "https://www.sec.gov/Archives/edgar/daily-index/{y}/QTR{q}/form.{d}.idx"
FORMS = {"10-K", "10-K/A", "10-Q", "10-Q/A", "10-KT", "10-KT/A", "10-QT", "10-QT/A"}
DIAS_INCREMENTAL = int(os.environ.get("DIAS_INCREMENTAL", "5"))
REQ_POR_SEG = float(os.environ.get("SEC_REQ_POR_SEG", "5"))      # SEC permite até 10/s; usamos metade
VALIDADE_CADASTRO_DIAS = 120
TOL = 6                                                          # tolerância (dias) para casar datas de períodos

TAGS_USD = todas_tags(DURACAO) | todas_tags(INSTANTE)
TAGS_SHARES = todas_tags(ACOES_INSTANTE) | todas_tags(ACOES_DURACAO)
FLUXOS = list(DURACAO)
SALDOS = list(INSTANTE)


# =============================================================================== HTTP
class Sec:
    def __init__(self):
        self.s = requests.Session()
        self.s.headers.update({"User-Agent": UA, "Accept-Encoding": "gzip, deflate"})
        self.ultimo = 0.0

    def get(self, url: str, stream: bool = False, tentativas: int = 5) -> requests.Response | None:
        for i in range(tentativas):
            espera = 1.0 / REQ_POR_SEG - (time.time() - self.ultimo)
            if espera > 0:
                time.sleep(espera)
            self.ultimo = time.time()
            try:
                r = self.s.get(url, timeout=120, stream=stream)
            except requests.RequestException as e:
                print(f"  rede: {e} ({url})")
                time.sleep(2 ** i)
                continue
            if r.status_code == 404:
                return None
            if r.status_code in (403, 429, 500, 502, 503):
                time.sleep(float(r.headers.get("Retry-After", 2 ** i)))
                continue
            r.raise_for_status()
            return r
        print(f"  falhou após {tentativas} tentativas: {url}")
        return None


# =============================================================================== datas
def _ord(s: str) -> int:
    return dt.date.fromisoformat(s).toordinal()


def _iso(o: int | None) -> str | None:
    return dt.date.fromordinal(o).isoformat() if o else None


def rotulo_fy(fim: int) -> int:
    """Exercício = ano civil do encerramento; anos de 52/53 semanas que fecham na 1ª semana de janeiro contam no ano anterior."""
    d = dt.date.fromordinal(fim)
    return d.year - 1 if (d.month == 1 and d.day <= 7) else d.year


# =============================================================================== fatos de uma empresa
def extrair(facts: dict) -> tuple[dict, dict, dict, dict, list, dict]:
    """
    Retorna (dur, inst, dur_acoes, inst_acoes, dei, primeiro) com o valor mais recente (último 'filed') por período.
      dur[tag]  = {fim: [(inicio, valor), ...]}        inst[tag] = {fim: valor}
    Só formulários 10-K/10-Q (e variantes). Só USD para monetários; 'shares' para ações.
    """
    gaap = facts.get("facts", {}).get("us-gaap", {})
    dei_f = facts.get("facts", {}).get("dei", {})

    primeiro = {}                                                # fim do período -> 1º arquivamento (divulgação original)

    def coletar(tag_map: set, unidade: str):
        dur_tmp, inst_tmp = defaultdict(dict), defaultdict(dict)
        for tag in tag_map:
            bloco = gaap.get(tag)
            if not bloco:
                continue
            for f in bloco.get("units", {}).get(unidade, []):
                if f.get("form") not in FORMS or f.get("val") is None:
                    continue
                fim = _ord(f["end"])
                filed = f.get("filed", "")
                if filed and (fim not in primeiro or filed < primeiro[fim]):
                    primeiro[fim] = filed
                if "start" in f:
                    chave = (_ord(f["start"]), fim)
                    ant = dur_tmp[tag].get(chave)
                    if ant is None or filed >= ant[1]:
                        dur_tmp[tag][chave] = (float(f["val"]), filed, f.get("form", ""))
                else:
                    ant = inst_tmp[tag].get(fim)
                    if ant is None or filed >= ant[1]:
                        inst_tmp[tag][fim] = (float(f["val"]), filed)
        dur = {}
        for tag, d in dur_tmp.items():
            idx = defaultdict(list)
            for (ini, fim), (v, _, form) in d.items():
                idx[fim].append((ini, v, form))
            dur[tag] = idx
        inst = {tag: {k: v[0] for k, v in d.items()} for tag, d in inst_tmp.items()}
        return dur, inst

    dur, inst = coletar(TAGS_USD, "USD")
    dur_a, inst_a = coletar(TAGS_SHARES, "shares")
    dei = []
    for f in dei_f.get(DEI_ACOES, {}).get("units", {}).get("shares", []):
        if f.get("form") in FORMS and f.get("val") is not None:
            dei.append((_ord(f["end"]), float(f["val"]), f.get("filed", "")))
    return dur, inst, dur_a, inst_a, dei, primeiro


def primeira_divulgacao(primeiro: dict, fim: int) -> str | None:
    """Data em que o período foi divulgado pela 1ª vez (comparativos e 10-K/A posteriores não contam)."""
    datas = [primeiro[f] for f in range(fim - TOL, fim + TOL + 1) if f in primeiro]
    return min(datas) if datas else None


def achar_dur(idx: dict, ini: int | None, fim: int | None) -> float | None:
    if ini is None or fim is None or not idx:
        return None
    melhor, dist = None, 99
    for f in range(fim - TOL, fim + TOL + 1):
        for (i, v, _) in idx.get(f, ()):
            d = abs(i - ini) + abs(f - fim)
            if abs(i - ini) <= TOL and d < dist:
                melhor, dist = v, d
    return melhor


def achar_inst(idx: dict, fim: int | None) -> float | None:
    if fim is None or not idx:
        return None
    for d in range(0, TOL + 1):
        for f in (fim - d, fim + d):
            if f in idx:
                return idx[f]
    return None


def valor_alt(fonte: dict, alt: list, ini, fim, instante: bool) -> float | None:
    """Âncora obrigatória; demais tags da alternativa somadas se existirem."""
    look = (lambda t: achar_inst(fonte.get(t), fim)) if instante else (lambda t: achar_dur(fonte.get(t), ini, fim))
    v = look(alt[0])
    if v is None:
        return None
    for t in alt[1:]:
        x = look(t)
        if x is not None:
            v += x
    return v


# =============================================================================== calendário fiscal
def calendario(dur: dict) -> list[dict]:
    """Lista de exercícios: {'ini','fim','fechado','fy','q_fim':[f1,f2,f3,f4]} (f4 = fim do exercício)."""
    anuais = Counter()
    todos = []                                                   # (ini, fim) de todos os fatos de duração
    for tag, idx in dur.items():
        for fim, lst in idx.items():
            for (ini, _, form) in lst:
                todos.append((ini, fim))
                if form.startswith("10-K") and 340 <= fim - ini <= 385:
                    anuais[(ini, fim)] += 1
    if not anuais:
        return []
    # agrupa por fim (±10 dias) e escolhe o par mais frequente
    grupos = []
    for (ini, fim), n in sorted(anuais.items(), key=lambda x: x[0][1]):
        if grupos and fim - grupos[-1][-1][0][1] <= 10:
            grupos[-1].append(((ini, fim), n))
        else:
            grupos.append([((ini, fim), n)])
    exs = [max(g, key=lambda x: x[1]) for g in grupos]
    # remove sobreposições (troca de exercício fiscal): fica o mais frequente
    limpos = []
    for (ini, fim), n in exs:
        if limpos and ini < limpos[-1][0][1] - 20:
            if n > limpos[-1][1]:
                limpos[-1] = ((ini, fim), n)
            continue
        limpos.append(((ini, fim), n))
    lista = [{"ini": i, "fim": f, "fechado": True} for (i, f), _ in limpos]
    # exercício em andamento (10-Q já publicado, 10-K ainda não)
    ult = lista[-1]["fim"]
    if any(abs(i - (ult + 1)) <= TOL and f > ult + 20 for (i, f) in todos):
        lista.append({"ini": ult + 1, "fim": ult + 364, "fechado": False})
    # fins de trimestre
    for ex in lista:
        S, E = ex["ini"], ex["fim"]
        candidatos = Counter()
        for (i, f) in todos:
            if S + 20 < f < E - 20 and (abs(i - S) <= TOL or 75 <= f - i <= 125):
                candidatos[f] += 1
        clusters = []
        for f in sorted(candidatos):
            if clusters and f - clusters[-1][-1] <= 7:
                clusters[-1].append(f)
            else:
                clusters.append([f])
        fins = [max(c, key=lambda x: candidatos[x]) for c in clusters]
        q = [None, None, None]
        for k in range(3):
            alvo = S + (k + 1) * (E - S + 1) / 4
            perto = [f for f in fins if abs(f - alvo) <= 30]
            if perto:
                q[k] = min(perto, key=lambda f: abs(f - alvo))
        ex["q_fim"] = q + ([E] if ex["fechado"] else [None])
        # início de cada trimestre: dia seguinte ao fim do anterior; se o anterior faltar, usa o início de um fato de 3M
        qi = [S, None, None, None]
        for k in range(1, 4):
            fk = ex["q_fim"][k]
            if ex["q_fim"][k - 1]:
                qi[k] = ex["q_fim"][k - 1] + 1
            elif fk:
                tres = [i for (i, f) in todos if abs(f - fk) <= TOL and 75 <= f - i <= 125]
                qi[k] = Counter(tres).most_common(1)[0][0] if tres else None
        ex["q_ini"] = qi
        ex["fy"] = rotulo_fy(E)
    return lista


# =============================================================================== processamento
def resolver_fluxo(dur: dict, alts: list, ex: dict) -> tuple[list, float | None, str, str]:
    """Trimestres discretos [q1..q4], valor anual, tag usada e método por trimestre (para 'fonte')."""
    S, E = ex["ini"], ex["fim"]
    qf, qi = ex["q_fim"], ex["q_ini"]

    def calcular(alt):
        anual = valor_alt(dur, alt, S, E, False) if ex["fechado"] else None
        cum = [valor_alt(dur, alt, S, qf[k], False) if k < 3 else anual for k in range(4)]
        q3m = [valor_alt(dur, alt, qi[k], qf[k], False) for k in range(4)]
        return anual, cum, q3m

    melhor, cob_max = None, 0
    for n, alt in enumerate(alts):
        anual, cum, q3m = calcular(alt)
        cob = sum(v is not None for v in [anual] + cum[:3] + q3m)
        if cob > cob_max:
            melhor, cob_max = (alt, anual, cum, q3m), cob
    if melhor is None:
        return [None] * 4, None, "", ""
    alt, anual, cum, q3m = melhor
    # completa lacunas com outras alternativas (registrado como 'misto')
    misto = False
    for outra in alts:
        if outra is alt:
            continue
        a2, c2, q2 = calcular(outra)
        if anual is None and a2 is not None:
            anual, misto = a2, True
        for k in range(4):
            if cum[k] is None and c2[k] is not None and k < 3:
                cum[k], misto = c2[k], True
            if q3m[k] is None and q2[k] is not None:
                q3m[k], misto = q2[k], True
    if ex["fechado"]:
        cum[3] = anual
    disc, metodo = [None] * 4, [""] * 4
    for k in range(4):
        if q3m[k] is not None:
            disc[k], metodo[k] = q3m[k], "3M reportado"
        elif k == 0 and cum[0] is not None:
            disc[k], metodo[k] = cum[0], "3M reportado"
        elif k > 0 and cum[k] is not None and cum[k - 1] is not None:
            disc[k], metodo[k] = cum[k] - cum[k - 1], ("anual - 9M" if k == 3 else "YTD diferenciado")
        elif k > 0 and cum[k] is not None and all(d is not None for d in disc[:k]):
            disc[k], metodo[k] = cum[k] - sum(disc[:k]), ("anual - soma 3T" if k == 3 else "YTD - trimestres")
    tag = "+".join(alt) + (" (misto)" if misto else "")
    return disc, anual, tag, ";".join(metodo)


def processar_empresa(facts: dict) -> tuple[pd.DataFrame, pd.DataFrame, dict]:
    dur, inst, dur_a, inst_a, dei, primeiro = extrair(facts)
    aud = {"exercicios": 0, "tags": "", "ausentes": "", "motivo": ""}
    if not dur and not inst:
        aud["motivo"] = "sem fatos US GAAP em USD (10-K/10-Q)"
        return pd.DataFrame(), pd.DataFrame(), aud
    cal = calendario(dur)
    if not cal:
        aud["motivo"] = "sem exercício anual identificável (nenhum 10-K com período de ~12 meses)"
        return pd.DataFrame(), pd.DataFrame(), aud

    tags_usadas = defaultdict(Counter)
    trimestres = []                      # [{ini, fim, fy, k, valores, metodo}]
    anuais = []
    for ex in cal:
        qf, qi = ex["q_fim"], ex["q_ini"]
        porq = [dict() for _ in range(4)]
        anual_v = {}
        metodo_receita = ""
        for c in FLUXOS:
            disc, anual, tag, metodo = resolver_fluxo(dur, DURACAO[c], ex)
            if tag:
                tags_usadas[c][tag] += 1
            for k in range(4):
                porq[k][c] = disc[k]
            anual_v[c] = anual
            if c == "receita" or (c == "ebit" and not metodo_receita):
                metodo_receita = metodo or metodo_receita
        for c in ACOES_DURACAO:
            _, anual, _, _ = resolver_fluxo(dur_a, ACOES_DURACAO[c], ex)
            anual_v[c] = anual
        met = metodo_receita.split(";") if metodo_receita else [""] * 4
        for k in range(4):
            if qf[k] is None or qi[k] is None:
                continue
            trimestres.append({"ini": qi[k], "fim": qf[k], "fy": ex["fy"], "k": k + 1, "v": porq[k],
                               "metodo": met[k] if k < len(met) else ""})
        if ex["fechado"]:
            anuais.append({"ini": ex["ini"], "fim": ex["fim"], "fy": ex["fy"], "v": anual_v})

    def saldos(fim: int) -> dict:
        out = {}
        for c in SALDOS:
            for alt in INSTANTE[c]:
                v = valor_alt(inst, alt, None, fim, True)
                if v is not None:
                    out[c] = v
                    tags_usadas[c]["+".join(alt)] += 1
                    break
            else:
                out[c] = None
        return out

    linhas = []
    for a in anuais:
        r = {"tipo": "anual", "exercicio": a["fy"], "trimestre": 4, "periodo": str(a["fy"]),
             "dt_ini": a["ini"], "dt_fim": a["fim"], "fonte": "10-K (anual)",
             "dt_arquivamento": primeira_divulgacao(primeiro, a["fim"])}
        r.update(a["v"])
        r.update(saldos(a["fim"]))
        linhas.append(r)
    for q in trimestres:
        r = {"tipo": "trimestral", "exercicio": q["fy"], "trimestre": q["k"],
             "periodo": f"{q['k']}T{str(q['fy'])[-2:]}", "dt_ini": q["ini"], "dt_fim": q["fim"],
             "fonte": q["metodo"], "dt_arquivamento": primeira_divulgacao(primeiro, q["fim"])}
        r.update(q["v"])
        r.update(saldos(q["fim"]))
        q["saldos"] = {c: r[c] for c in SALDOS}
        linhas.append(r)
    # LTM: soma de 4 trimestres contíguos; no 4T usa o anual se faltar algum trimestre
    anual_por_fim = {a["fim"]: a for a in anuais}
    for i in range(3, len(trimestres)):
        bloco = trimestres[i - 3:i + 1]
        if not all(0 <= bloco[j]["ini"] - bloco[j - 1]["fim"] <= 10 for j in range(1, 4)):
            continue
        if not 340 <= bloco[-1]["fim"] - bloco[0]["ini"] <= 385:
            continue
        q = bloco[-1]
        r = {"tipo": "ltm", "exercicio": q["fy"], "trimestre": q["k"],
             "periodo": f"LTM {q['k']}T{str(q['fy'])[-2:]}", "dt_ini": bloco[0]["ini"], "dt_fim": q["fim"],
             "fonte": "soma 4 trimestres", "dt_arquivamento": primeira_divulgacao(primeiro, q["fim"])}
        an = anual_por_fim.get(q["fim"]) if q["k"] == 4 else None
        usou_anual = False
        for c in FLUXOS:
            vals = [b["v"].get(c) for b in bloco]
            if all(v is not None for v in vals):
                r[c] = sum(vals)
            elif an is not None and an["v"].get(c) is not None:
                r[c], usou_anual = an["v"][c], True
            else:
                r[c] = None
        if usou_anual:
            r["fonte"] = "soma 4 trimestres / anual"
        r.update(q.get("saldos", {}))
        linhas.append(r)

    df = pd.DataFrame(linhas)
    if df.empty:
        aud["motivo"] = "sem linhas"
        return df, pd.DataFrame(), aud
    # descarta linhas totalmente vazias
    num = [c for c in FLUXOS + SALDOS if c in df.columns]
    df = df[df[num].notna().any(axis=1)].copy()
    aud["exercicios"] = int((df["tipo"] == "anual").sum())
    aud["tags"] = "; ".join(f"{c}:{t.most_common(1)[0][0]}" for c, t in tags_usadas.items() if t)
    aud["ausentes"] = ", ".join(c for c in ["receita", "ebit", "da", "fco", "ativo_total", "patrimonio_liquido"]
                                if c not in tags_usadas)
    acoes = serie_acoes(inst_a, dur_a, dei)
    return df, acoes, aud


def serie_acoes(inst_a: dict, dur_a: dict, dei: list) -> pd.DataFrame:
    """Ações em circulação. Prioridade: capa (dei) > balanço > média básica 3M > média diluída 3M.
    Empresas multiclasse costumam não ter o dado de capa no companyfacts (vem com dimensão por classe);
    capa < 70% do balanço mais próximo => trata como parcial e usa o balanço."""
    balanco = inst_a.get("CommonStockSharesOutstanding", {})
    medias = {}
    for tag, nome in (("WeightedAverageNumberOfSharesOutstandingBasic", "media_basica"),
                      ("WeightedAverageNumberOfDilutedSharesOutstanding", "media_diluida")):
        for fim, lst in dur_a.get(tag, {}).items():
            tri = [v for (i, v, _) in lst if 75 <= fim - i <= 125]
            if tri:
                medias.setdefault(fim, {})[nome] = tri[0]
    capa = {}
    for fim, v, filed in dei:
        if fim not in capa or filed >= capa[fim][1]:
            capa[fim] = (v, filed)
    datas = sorted(set(capa) | set(balanco) | set(medias))
    linhas = []
    for d in datas:
        c = capa.get(d, (None,))[0]
        b = balanco.get(d)
        bal_prox = None
        if c is not None and b is None:
            ant = [x for x in balanco if d - 200 <= x <= d]
            bal_prox = balanco[max(ant)] if ant else None
        mb = medias.get(d, {}).get("media_basica")
        md = medias.get(d, {}).get("media_diluida")
        if c is not None:
            ref = b if b is not None else bal_prox
            if ref and c < 0.7 * ref:
                escolha, fonte = ref, "balanço (capa parcial: possível multiclasse)"
            else:
                escolha, fonte = c, "capa (dei)"
        elif b is not None:
            escolha, fonte = b, "balanço"
        elif mb is not None:
            escolha, fonte = mb, "média básica 3M"
        elif md is not None:
            escolha, fonte = md, "média diluída 3M"
        else:
            continue
        linhas.append({"data": d, "acoes": escolha / 1e6, "fonte_acoes": fonte,
                       "acoes_capa": None if c is None else c / 1e6, "acoes_balanco": None if b is None else b / 1e6,
                       "acoes_media_basica": None if mb is None else mb / 1e6})
    a = pd.DataFrame(linhas)
    if not a.empty:
        razao = a["acoes"] / a["acoes"].shift(1)
        a["alerta_salto"] = (razao > 5) | (razao < 0.2)
    return a


# =============================================================================== métricas derivadas
def derivar(df: pd.DataFrame) -> pd.DataFrame:
    m = 1e6
    for c in FLUXOS + SALDOS:
        if c not in df.columns:
            df[c] = np.nan
        df[c] = pd.to_numeric(df[c], errors="coerce") / m
    for c in ("acoes_media_basica", "acoes_media_diluida"):
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce") / m
    z = lambda s: s.fillna(0)
    notas = pd.Series("", index=df.index)

    sem_lb = df["lucro_bruto"].isna() & df["receita"].notna() & df["cpv"].notna()
    df.loc[sem_lb, "lucro_bruto"] = df["receita"] - df["cpv"]
    notas[sem_lb] += "lucro bruto = receita - cpv; "

    df["ebitda"] = df["ebit"] + df["da"] + z(df["impairment"])
    df["pagamento_arrendamento"] = z(df["pag_arrend_principal"]) + z(df["pag_arrend_juros"])
    df["ebitda_pre_ifrs16"] = df["ebitda"] - df["pagamento_arrendamento"]
    df["ebitda_ifrs16_equiv"] = df["ebitda"] + df["custo_arrend_operacional"]
    sem_op = df["custo_arrend_operacional"].isna() & (z(df["passivo_arrend_operacional"]) == 0)
    df.loc[sem_op, "ebitda_ifrs16_equiv"] = df["ebitda"]          # sem arrendamento operacional: equivalente = EBITDA
    df["regime_contabil"] = "US GAAP (ASC 842)"
    df["base"] = "consolidado"

    capex_vazio = df["capex_imobilizado"].isna() & df["capex_intangivel"].isna()
    df["capex_organico"] = (z(df["capex_imobilizado"]) + z(df["capex_intangivel"])).mask(capex_vazio)
    df["capex_organico_liquido"] = df["capex_organico"] - z(df["vendas_imob_intang"])
    df["capex_total"] = df["capex_organico"] + z(df["aquisicoes_participacoes"])
    df["capex_total_liquido"] = df["capex_total"] - z(df["vendas_imob_intang"])
    df["fcl_pos_arrend"] = df["fco"] - df["capex_organico"] - z(df["pag_arrend_principal"])

    df["resultado_financeiro"] = z(df["receitas_financeiras"]) - df["despesas_financeiras"]
    df["desp_fin_liquida"] = -df["resultado_financeiro"]

    df["caixa"] = df["caixa_equiv"] + z(df["aplicacoes_cp"])
    sem_pt = df["passivo_total"].isna() & df["passivo_e_pl"].notna() & df["patrimonio_liquido"].notna()
    df.loc[sem_pt, "passivo_total"] = df["passivo_e_pl"] - df["patrimonio_liquido"]
    notas[sem_pt] += "passivo total = passivo+PL - PL; "
    sem_pnc = df["passivo_nao_circulante"].isna() & df["passivo_total"].notna() & df["passivo_circulante"].notna()
    df.loc[sem_pnc, "passivo_nao_circulante"] = df["passivo_total"] - df["passivo_circulante"]

    df["passivo_arrendamento"] = z(df["passivo_arrend_operacional"]) + z(df["passivo_arrend_financeiro"])
    df["divida_bruta_ex_arrend"] = df["emprestimos_financiamentos"]
    df["div_liq_sem_arrend"] = df["emprestimos_financiamentos"] - df["caixa"]
    df["div_liq_com_arrend"] = df["div_liq_sem_arrend"] + df["passivo_arrendamento"]
    df["ncav"] = df["ativo_circulante"] - df["passivo_total"]

    def div(a, b):
        b = b.where(b != 0)
        return a / b

    df["margem_bruta"] = div(df["lucro_bruto"], df["receita"])
    df["margem_ebitda"] = div(df["ebitda"], df["receita"])
    df["margem_ebitda_pre_ifrs16"] = div(df["ebitda_pre_ifrs16"], df["receita"])
    df["margem_liquida"] = div(df["lucro_liquido"], df["receita"])
    df["capex_org_receita"] = div(df["capex_organico"], df["receita"])
    df["capex_org_da"] = div(df["capex_organico"], df["da"])

    df["ebitda_menos_ir"] = df["ebitda"] - z(df["ir_csll_pagos"])
    df["servico_divida_pago"] = df["juros_pagos"] + z(df["principal_pago"])
    df["servico_divida_c_arrend"] = df["servico_divida_pago"] + df["pagamento_arrendamento"]
    df["ebitda_ir_capex"] = df["ebitda_menos_ir"] - z(df["capex_organico"])
    df["cob_ebit_res_fin"] = div(df["ebit"], df["desp_fin_liquida"].where(df["desp_fin_liquida"] > 0))
    df["cob_juros_caixa"] = div(df["ebitda_menos_ir"], df["juros_pagos"])
    df["dscr_ebitda"] = div(df["ebitda_menos_ir"], df["servico_divida_pago"])
    df["dscr_pos_capex"] = div(df["ebitda_ir_capex"], df["servico_divida_pago"])
    df["dscr_ebitda_arrend_den"] = div(df["ebitda_menos_ir"], df["servico_divida_c_arrend"])
    df["retorno_acionista_caixa"] = z(df["dividendos_pagos"]) + z(df["recompra_acoes"]) - z(df["emissao_acoes"])

    # índices de estoque/fluxo só fazem sentido em 12 meses
    doze = df["tipo"].isin(["anual", "ltm"])
    df["div_liq_ebitda_ifrs16"] = div(df["div_liq_com_arrend"], df["ebitda_ifrs16_equiv"]).where(doze)
    df["div_liq_ebitda_pre_ifrs16"] = div(df["div_liq_sem_arrend"], df["ebitda_pre_ifrs16"]).where(doze)
    df["roe_pl_final"] = div(df["lucro_controladores"], df["pl_controladores"]).where(doze)

    # ROIC (variante Price Discovery ajustada ao US GAAP / ASC 842), calculado aqui para o app não gastar memória.
    # NOPAT = EBIT × (1 − 25%) se EBIT > 0 (21% federal + média estadual; sem crédito fiscal sobre prejuízo).
    # Arrendamento operacional já está no EBIT (opex); só o financeiro segue a lógica do IFRS 16.
    # ROIC caixa = (NOPAT + D&A − capex org. − pagamentos de arrend. financeiro) ÷ (DL s/ arrend. + PL); sem
    #   pagamento de arrend. financeiro na DFC e com passivo financeiro, 2,5 × esse passivo entra no capital.
    # ROIC tradicional = NOPAT aj. ÷ (DL s/ arrend. + passivos de arrend. financeiro e operacional + PL), com
    #   NOPAT aj. sobre EBIT + juros implícitos do arrend. operacional (passivo operacional × juros pagos ÷ dívida
    #   bruta, entre 3% e 12%; sem dado, 6%; nunca acima do custo de arrend. operacional divulgado).
    # ROIC caixa ex-goodwill = mesmo numerador ÷ (CI do ROIC caixa − goodwill).
    # Capital no fim do período; só anual e LTM; vazio com capital ≤ 0.
    aliq_ir = 0.25
    p_fin, p_op = z(df["passivo_arrend_financeiro"]), z(df["passivo_arrend_operacional"])
    nopat = df["ebit"].where(df["ebit"] <= 0, df["ebit"] * (1 - aliq_ir))
    capitaliza = df["pag_arrend_principal"].isna() & df["pag_arrend_juros"].isna() & (p_fin > 0)
    ci_caixa = df["div_liq_sem_arrend"] + df["patrimonio_liquido"] + (2.5 * p_fin).where(capitaliza, 0)
    num_caixa = nopat + df["da"] - df["capex_organico"] - df["pagamento_arrendamento"]
    divida = df["divida_bruta_ex_arrend"]
    taxa_op = (df["juros_pagos"].abs() / divida.where(divida > 0)).clip(0.03, 0.12).fillna(0.06)
    juros_op = p_op * taxa_op
    custo_op = df["custo_arrend_operacional"]
    juros_op = juros_op.where(custo_op.isna(), np.minimum(juros_op, custo_op.clip(lower=0)))
    ebit_aj = df["ebit"] + juros_op
    nopat_aj = ebit_aj.where(ebit_aj <= 0, ebit_aj * (1 - aliq_ir))
    ci_trad = df["div_liq_sem_arrend"] + p_fin + p_op + df["patrimonio_liquido"]
    df["roic_caixa"] = div(num_caixa, ci_caixa.where(ci_caixa > 0)).where(doze)
    df["roic_trad"] = div(nopat_aj, ci_trad.where(ci_trad > 0)).where(doze)
    # ROIC caixa ex-goodwill: mesmo numerador do ROIC caixa; do capital sai o goodwill do balanço (tag Goodwill,
    # ASC 350). Sem a tag = sem goodwill (0). Intangíveis identificados na aquisição continuam no capital.
    ci_caixa_ex_gw = ci_caixa - z(df["goodwill"])
    df["roic_caixa_ex_goodwill"] = div(num_caixa, ci_caixa_ex_gw.where(ci_caixa_ex_gw > 0)).where(doze)

    notas[df["receita"].isna()] += "receita ausente; "
    notas[df["da"].isna() & df["ebit"].notna()] += "D&A ausente (sem EBITDA); "
    notas[df["emprestimos_financiamentos"].isna()] += "dívida não identificada; "
    df["notas"] = notas.str.strip("; ")
    return df


COLUNAS = [
    "empresa", "ticker", "setor", "subsetor", "segmento", "cnpj", "tipo", "exercicio", "trimestre", "periodo",
    "dt_ini", "dt_fim", "dt_arquivamento", "fonte", "base", "regime_contabil",
    "receita", "lucro_bruto", "ebit", "lucro_liquido", "lucro_controladores", "cpv", "da", "impairment", "ebitda",
    "pagamento_arrendamento", "pag_arrend_juros", "pag_arrend_principal", "ebitda_pre_ifrs16", "ebitda_ifrs16_equiv",
    "custo_arrend_operacional", "pag_arrend_operacional",
    "fco", "capex_imobilizado", "capex_intangivel", "capex_organico", "vendas_imob_intang", "capex_organico_liquido",
    "aquisicoes_participacoes", "capex_total", "capex_total_liquido", "fcl_pos_arrend", "dividendos_pagos",
    "recompra_acoes", "emissao_acoes", "retorno_acionista_caixa",
    "resultado_financeiro", "receitas_financeiras", "despesas_financeiras", "ir_csll_pagos", "juros_pagos",
    "principal_pago", "servico_divida_pago",
    "ativo_total", "ativo_circulante", "caixa", "passivo_total", "passivo_circulante", "passivo_nao_circulante",
    "patrimonio_liquido", "emprestimos_financiamentos", "passivo_arrendamento", "passivo_arrend_operacional",
    "passivo_arrend_financeiro", "divida_bruta_ex_arrend", "pl_controladores", "nao_controladores", "clientes",
    "estoques", "fornecedores", "goodwill", "div_liq_com_arrend", "div_liq_sem_arrend", "ncav",
    "margem_bruta", "margem_ebitda", "margem_ebitda_pre_ifrs16", "margem_liquida", "capex_org_receita",
    "capex_org_da", "div_liq_ebitda_ifrs16", "div_liq_ebitda_pre_ifrs16", "roe_pl_final", "roic_caixa", "roic_trad",
    "roic_caixa_ex_goodwill", "desp_fin_liquida",
    "ebitda_menos_ir", "ebitda_ir_capex", "servico_divida_c_arrend", "cob_ebit_res_fin",
    "cob_juros_caixa", "dscr_ebitda", "dscr_pos_capex", "dscr_ebitda_arrend_den",
    "acoes_media_basica", "acoes_media_diluida", "notas",
    "cik", "tickers", "bolsa", "tipo_especial", "categoria_sec",
]


def montar(df: pd.DataFrame, cad: dict) -> pd.DataFrame:
    df = derivar(df)
    for k in ("empresa", "ticker", "tickers", "bolsa", "setor", "subsetor", "segmento", "tipo_especial",
              "categoria_sec"):
        df[k] = cad.get(k, "")
    df["cik"] = int(cad["cik"])
    df["cnpj"] = f"CIK{int(cad['cik']):010d}"          # chave textual equivalente ao CNPJ da base BR
    df["dt_ini"] = df["dt_ini"].map(_iso)
    df["dt_fim"] = df["dt_fim"].map(_iso)
    for c in COLUNAS:
        if c not in df.columns:
            df[c] = np.nan
    return df[COLUNAS]


# =============================================================================== cadastro / universo
def carregar_universo(sec: Sec) -> pd.DataFrame:
    r = sec.get(URL_TICKERS)
    j = r.json()
    t = pd.DataFrame(j["data"], columns=j["fields"])
    t = t[t["exchange"].isin(BOLSAS)]
    g = t.groupby("cik", sort=False).agg(nome=("name", "first"), ticker=("ticker", "first"),
                                         tickers=("ticker", lambda s: "|".join(s)), bolsa=("exchange", "first"))
    return g.reset_index()


def atualizar_cadastro(sec: Sec, univ: pd.DataFrame, limite: int | None) -> pd.DataFrame:
    arq = CACHE / "submissions_meta.csv"
    meta = pd.read_csv(arq, dtype={"sic": str}) if arq.exists() else pd.DataFrame(
        columns=["cik", "sic", "sic_desc", "owner_org", "fye", "categoria_sec", "entity_type", "atualizado_em"])
    hoje = dt.date.today()
    conhecidos = dict(zip(meta["cik"], meta["atualizado_em"]))
    pend = [c for c in univ["cik"] if c not in conhecidos]
    velhos = [c for c in univ["cik"] if c in conhecidos and
              (hoje - dt.date.fromisoformat(str(conhecidos[c]))).days > VALIDADE_CADASTRO_DIAS]
    fila = pend + velhos
    if limite is not None:
        fila = fila[:limite]
    print(f"cadastro: {len(pend)} novos, {len(velhos)} vencidos; consultando {len(fila)}")
    novos = []
    for n, cik in enumerate(fila, 1):
        r = sec.get(URL_SUBM.format(int(cik)))
        if r is None:
            continue
        s = r.json()
        novos.append({"cik": int(cik), "sic": str(s.get("sic") or ""), "sic_desc": s.get("sicDescription") or "",
                      "owner_org": s.get("ownerOrg") or "", "fye": s.get("fiscalYearEnd") or "",
                      "categoria_sec": s.get("category") or "", "entity_type": s.get("entityType") or "",
                      "atualizado_em": hoje.isoformat()})
        if n % 500 == 0:
            print(f"  {n}/{len(fila)}")
    if novos:
        meta = pd.concat([meta[~meta["cik"].isin([x["cik"] for x in novos])], pd.DataFrame(novos)])
        CACHE.mkdir(exist_ok=True)
        meta.to_csv(arq, index=False)
    cad = univ.merge(meta, on="cik", how="left")
    cad["empresa"] = cad["nome"]
    cad["setor"] = cad["owner_org"].map(setor_de)
    cad["subsetor"] = cad["sic"].map(subsetor_de)
    cad["segmento"] = cad["sic_desc"].fillna("").replace("", "Não classificado")
    cad["tipo_especial"] = cad["sic"].map(tipo_especial)
    return cad


def no_universo(cad_row) -> tuple[bool, str]:
    if cad_row.get("tipo_especial") == "SPAC":
        return False, "SPAC (SIC 6770)"
    et = str(cad_row.get("entity_type") or "")
    if et == "other":
        return False, "emissor estrangeiro (20-F/40-F) ou não operacional (entityType=other)"
    if et and et != "operating":
        return False, f"entityType={et}"
    return True, ""


def _texto(v, largura: int = 0) -> str:
    """Texto limpo para o parquet do cadastro: o cache em CSV volta como número (1231 -> 1231.0) e as empresas
    novas entram como texto; tipo misto na mesma coluna quebra o to_parquet (falhas de 10 a 15/11/2026)."""
    if v is None or (isinstance(v, float) and np.isnan(v)):
        return ""
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    t = str(v).strip()
    return t.zfill(largura) if largura and t.isdigit() else t


# =============================================================================== execução
def salvar(base: pd.DataFrame, acoes: pd.DataFrame, cad: pd.DataFrame, aud: pd.DataFrame, modo: str, extra: dict):
    DADOS.mkdir(exist_ok=True)
    tam = {}
    for tipo in ("anual", "trimestral", "ltm"):
        arq = DADOS / f"eua_{tipo}.parquet"
        parte = base[base["tipo"] == tipo].sort_values(["setor", "subsetor", "segmento", "empresa", "dt_fim"])
        parte.to_parquet(arq, index=False, compression="zstd")
        tam[tipo] = round(arq.stat().st_size / 1e6, 1)
        if tam[tipo] > 90:
            print(f"ATENÇÃO: {arq.name} com {tam[tipo]} MB (limite do GitHub: 100 MB por arquivo)")
    acoes.to_parquet(DADOS / "acoes_eua.parquet", index=False, compression="zstd")
    cols_cad = ["cik", "empresa", "ticker", "tickers", "bolsa", "sic", "sic_desc", "owner_org", "setor", "subsetor",
                "segmento", "tipo_especial", "fye", "categoria_sec", "entity_type"]
    cad_out = cad[[c for c in cols_cad if c in cad.columns]].copy()
    for c in cad_out.columns:  # cache CSV relido como número + empresas novas em texto: tipo misto quebra o parquet
        if cad_out[c].dtype == object and not cad_out[c].map(lambda v: isinstance(v, str)).all():
            cad_out[c] = cad_out[c].map(lambda v, c=c: _texto(v, 4 if c in ("sic", "fye") else 0))
    cad_out.to_parquet(DADOS / "cadastro_eua.parquet", index=False)
    aud.to_csv(DADOS / "auditoria_eua.csv.gz", index=False)
    meta = {"gerado_em": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC"), "modo": modo,
            "empresas_universo": int(len(cad)), "empresas_com_dados": int(base["cik"].nunique()),
            "linhas": {t: int((base["tipo"] == t).sum()) for t in ("anual", "trimestral", "ltm")},
            "tamanho_mb": tam, "unidade": "US$ milhões; ações em milhões",
            "fonte": "SEC EDGAR XBRL companyfacts + submissions + company_tickers_exchange"}
    meta.update(extra)
    (DADOS / "metadados.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2))
    print(json.dumps(meta, ensure_ascii=False, indent=2))


def processar_lote(itens, cad_idx: dict) -> tuple[list, list, list]:
    """itens: iterável de (cik, facts_dict | None)."""
    bases, acoes, auds = [], [], []
    for n, (cik, facts) in enumerate(itens, 1):
        c = cad_idx[cik]
        reg = {"cik": cik, "ticker": c.get("ticker"), "empresa": c.get("empresa"), "setor": c.get("setor")}
        if facts is None:
            reg["motivo"] = "sem companyfacts na SEC"
            auds.append(reg)
            continue
        try:
            df, ac, aud = processar_empresa(facts)
        except Exception as e:                                        # empresa problemática não derruba o lote
            reg["motivo"] = f"erro: {type(e).__name__}: {e}"
            auds.append(reg)
            continue
        reg.update(aud)
        if not df.empty:
            reg["linhas"] = len(df)
            reg["ultimo_dt_fim"] = _iso(int(df["dt_fim"].max()))
            bases.append(montar(df.copy(), c))
            if not ac.empty:
                ac.insert(0, "cik", cik)
                ac["data"] = ac["data"].map(_iso)
                acoes.append(ac)
        auds.append(reg)
        if n % 500 == 0:
            print(f"  processadas {n}")
    return bases, acoes, auds


def modo_completo(sec: Sec):
    univ = carregar_universo(sec)
    cad = atualizar_cadastro(sec, univ, limite=None)
    cad_idx = {int(r["cik"]): r for r in cad.to_dict("records")}
    zip_path = Path(os.environ.get("TMPDIR", "/tmp")) / "companyfacts.zip"
    print("baixando companyfacts.zip ...")
    t0 = time.time()
    r = sec.get(URL_ZIP, stream=True)
    if r is None:
        sys.exit("não foi possível baixar companyfacts.zip")
    zip_mod = r.headers.get("Last-Modified", "")
    with open(zip_path, "wb") as f:
        for bloco in r.iter_content(chunk_size=8 << 20):
            f.write(bloco)
    print(f"  {zip_path.stat().st_size / 1e9:.2f} GB em {time.time() - t0:.0f}s")

    excl = []
    with zipfile.ZipFile(zip_path) as zf:
        nomes = set(zf.namelist())

        def itens():
            for cik, c in cad_idx.items():
                ok, motivo = no_universo(c)
                if not ok:
                    excl.append({"cik": cik, "ticker": c.get("ticker"), "empresa": c.get("empresa"),
                                 "setor": c.get("setor"), "motivo": motivo})
                    continue
                nome = f"CIK{cik:010d}.json"
                yield cik, (orjson.loads(zf.read(nome)) if nome in nomes else None)

        t0 = time.time()
        bases, acoes, auds = processar_lote(itens(), cad_idx)
        print(f"processamento: {time.time() - t0:.0f}s")
    zip_path.unlink(missing_ok=True)
    base = pd.concat(bases, ignore_index=True)
    ac = pd.concat(acoes, ignore_index=True) if acoes else pd.DataFrame()
    salvar(base, ac, cad, pd.DataFrame(auds + excl), "completo", {"companyfacts_zip_last_modified": zip_mod})


def ciks_recentes(sec: Sec, dias: int) -> set:
    ciks = set()
    hoje = dt.date.today()
    for d in range(1, dias + 1):
        x = hoje - dt.timedelta(days=d)
        if x.weekday() >= 5:
            continue
        url = URL_IDX.format(y=x.year, q=(x.month - 1) // 3 + 1, d=x.strftime("%Y%m%d"))
        r = sec.get(url)
        if r is None:
            continue
        for linha in r.text.splitlines():
            m = re.match(r"^(10-[KQ]T?(?:/A)?)\s{2,}.+?\s{2,}(\d+)\s{2,}\d{8}\s", linha)
            if m:
                ciks.add(int(m.group(2)))
    return ciks


def modo_incremental(sec: Sec):
    arqs = [DADOS / f"eua_{t}.parquet" for t in ("anual", "trimestral", "ltm")]
    if not all(a.exists() for a in arqs):
        print("sem base anterior: rodando modo completo")
        return modo_completo(sec)
    univ = carregar_universo(sec)
    cad = atualizar_cadastro(sec, univ, limite=400)
    cad_idx = {int(r["cik"]): r for r in cad.to_dict("records")}
    alvo = sorted(c for c in ciks_recentes(sec, DIAS_INCREMENTAL) if c in cad_idx and no_universo(cad_idx[c])[0])
    print(f"incremental: {len(alvo)} empresas com 10-K/10-Q nos últimos {DIAS_INCREMENTAL} dias")
    if not alvo:
        return

    def itens():
        for cik in alvo:
            r = sec.get(URL_FACTS.format(cik))
            yield cik, (r.json() if r is not None else None)

    bases, acoes, auds = processar_lote(itens(), cad_idx)
    base = pd.concat([pd.read_parquet(a) for a in arqs], ignore_index=True)
    base = base[~base["cik"].isin(alvo)]
    if bases:
        base = pd.concat([base] + bases, ignore_index=True)
    ac = pd.read_parquet(DADOS / "acoes_eua.parquet") if (DADOS / "acoes_eua.parquet").exists() else pd.DataFrame()
    if not ac.empty:
        ac = ac[~ac["cik"].isin(alvo)]
    ac = pd.concat([ac] + acoes, ignore_index=True) if acoes else ac
    aud_arq = DADOS / "auditoria_eua.csv.gz"
    aud = pd.read_csv(aud_arq) if aud_arq.exists() else pd.DataFrame()
    if not aud.empty:
        aud = aud[~aud["cik"].isin(alvo)]
    aud = pd.concat([aud, pd.DataFrame(auds)], ignore_index=True)
    meta_ant = json.loads((DADOS / "metadados.json").read_text()) if (DADOS / "metadados.json").exists() else {}
    salvar(base, ac, cad, aud, "incremental",
           {"empresas_atualizadas": len(alvo),
            "ultimo_completo": meta_ant.get("gerado_em") if meta_ant.get("modo") == "completo"
            else meta_ant.get("ultimo_completo")})


def conferencia(tickers=("AAPL", "MSFT", "WMT", "KO", "DAL", "GOOGL")):
    """Imprime números-chave de empresas conhecidas para checagem manual no log do Actions."""
    arq = DADOS / "eua_anual.parquet"
    if not arq.exists():
        return
    a = pd.read_parquet(arq)
    cols = ["ticker", "periodo", "dt_fim", "receita", "ebit", "lucro_controladores", "da", "ebitda", "fco",
            "capex_organico", "emprestimos_financiamentos", "caixa", "passivo_arrendamento", "patrimonio_liquido"]
    sel = a[a["ticker"].isin(tickers)].sort_values(["ticker", "dt_fim"]).groupby("ticker").tail(2)
    with pd.option_context("display.width", 250, "display.max_columns", 30):
        print("\n=== conferência (US$ milhões) ===")
        print(sel[cols].round(0).to_string(index=False))


if __name__ == "__main__":
    modo = sys.argv[1] if len(sys.argv) > 1 else "incremental"
    sec = Sec()
    if modo == "completo":
        modo_completo(sec)
    elif modo == "incremental":
        modo_incremental(sec)
    else:
        sys.exit(f"modo desconhecido: {modo}")
    conferencia()
