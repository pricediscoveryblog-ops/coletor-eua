"""
Calendário de resultados dos EUA (Price Discovery Scanning USA, aba "Calendário de resultados").

Duas saídas na pasta dados/ (branch dados):

  calendario_eua.parquet   divulgações de resultado (press release), passadas e agendadas
      Fonte: calendário de resultados da Nasdaq (api.nasdaq.com/api/calendar/earnings?date=AAAA-MM-DD),
      gratuito e sem chave. Cobre empresas com estimativa de analistas (não todo o universo da SEC).
      Dias passados trazem EPS realizado e surpresa; dias futuros trazem consenso de EPS, nº de estimativas
      e a data e o EPS do mesmo trimestre do ano anterior. A Nasdaq não informa se a data futura é confirmada
      pela empresa ou estimada.
      Histórico acumulado: a 1a execução busca ~400 dias para trás; as seguintes rebuscam de hoje-14 a
      hoje+90 dias e substituem esses dias (datas remarcadas somem do dia antigo).

  entregas_eua.parquet     data da 1a entrega do 10-Q/10-K de cada trimestre à SEC (todo o universo, inclusive
      bancos e seguradoras), extraída de eua_trimestral.parquet (coluna dt_arquivamento). Nos EUA o press
      release costuma sair antes do 10-Q/10-K; por isso as duas datas ficam separadas.

Se a Nasdaq falhar, o arquivo anterior é mantido (o passo do workflow é continue-on-error).
"""
from __future__ import annotations

import json
import re
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import requests

DADOS = Path("dados")
ARQ_CAL = DADOS / "calendario_eua.parquet"
ARQ_ENT = DADOS / "entregas_eua.parquet"
ARQ_META = DADOS / "calendario_meta.json"
URL = "https://api.nasdaq.com/api/calendar/earnings"
CABEC = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/131.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Origin": "https://www.nasdaq.com",
    "Referer": "https://www.nasdaq.com/",
}
DIAS_ATRAS_INICIAL = 400
DIAS_ATRAS = 14
DIAS_FRENTE = 90
HORARIO = {"time-pre-market": "Antes da abertura", "time-after-hours": "Após o fechamento",
           "time-not-supplied": "Não informado"}


def _num(s) -> float | None:
    """'$2.41' / '($0.12)' / '-33.33' / '$162,303,836,000' / 'N/A' -> float."""
    if s is None:
        return None
    t = str(s).strip()
    if not t or t.upper() in ("N/A", "NA", "--", "-"):
        return None
    neg = t.startswith("(") and t.endswith(")")
    t = re.sub(r"[^0-9.\-]", "", t)
    try:
        v = float(t)
    except ValueError:
        return None
    return -abs(v) if neg else v


def _data_us(s) -> str | None:
    try:
        return datetime.strptime(str(s).strip(), "%m/%d/%Y").date().isoformat()
    except (ValueError, TypeError):
        return None


def buscar_dia(sess: requests.Session, d: date) -> list[dict] | None:
    """Linhas de um dia; None se a requisição falhar (lista vazia = dia sem divulgações)."""
    for tentativa in range(3):
        try:
            r = sess.get(URL, params={"date": d.isoformat()}, timeout=25)
            if r.status_code == 200:
                j = r.json()
                return ((j.get("data") or {}).get("rows")) or []
        except (requests.RequestException, ValueError):
            pass
        time.sleep(2 * (tentativa + 1))
    return None


def linhas_dia(d: date, rows: list[dict], agora: str) -> list[dict]:
    out = []
    for x in rows:
        tk = str(x.get("symbol") or "").strip().upper()
        if not tk:
            continue
        out.append({
            "data": d.isoformat(),
            "ticker": tk,
            "empresa_nasdaq": str(x.get("name") or "").strip(),
            "horario": HORARIO.get(str(x.get("time") or ""), "Não informado"),
            "trimestre_fiscal": str(x.get("fiscalQuarterEnding") or "").strip(),
            "eps_consenso": _num(x.get("epsForecast")),
            "n_estimativas": _num(x.get("noOfEsts")),
            "eps_real": _num(x.get("eps")),
            "surpresa_pct": _num(x.get("surprise")),
            "data_ano_anterior": _data_us(x.get("lastYearRptDt")),
            "eps_ano_anterior": _num(x.get("lastYearEPS")),
            "valor_mercado_usd": _num(x.get("marketCap")),
            "coletado_em": agora,
        })
    return out


def atualizar_calendario() -> dict:
    hoje = datetime.now(timezone.utc).date()
    agora = datetime.now(timezone.utc).isoformat(timespec="seconds")
    antigo = pd.read_parquet(ARQ_CAL) if ARQ_CAL.exists() else pd.DataFrame()
    atras = DIAS_ATRAS if len(antigo) else DIAS_ATRAS_INICIAL
    dias = [hoje + timedelta(days=k) for k in range(-atras, DIAS_FRENTE + 1)]
    sess = requests.Session()
    sess.headers.update(CABEC)
    novas, ok, falhas = [], [], []
    for i, d in enumerate(dias):
        rows = buscar_dia(sess, d)
        if rows is None:
            falhas.append(d.isoformat())
            if i >= 4 and len(falhas) == i + 1:          # as 5 primeiras falharam: Nasdaq bloqueou, desiste
                print("Nasdaq não respondeu nas 5 primeiras datas; mantendo o arquivo anterior.")
                break
        else:
            ok.append(d.isoformat())
            novas += linhas_dia(d, rows, agora)
        time.sleep(0.35)
    print(f"calendário: {len(ok)} dias lidos, {len(falhas)} falhas, {len(novas)} linhas")
    if not ok:
        return {"calendario_ok": False, "dias_lidos": 0, "dias_falha": len(falhas)}
    novo = pd.DataFrame(novas)
    if len(antigo):
        antigo = antigo[~antigo["data"].isin(ok)]             # dias rebuscados são substituídos inteiros
        cal = pd.concat([antigo, novo], ignore_index=True) if len(novo) else antigo
    else:
        cal = novo
    if len(cal):
        cal = cal.sort_values(["data", "ticker"]).drop_duplicates(["data", "ticker"], keep="last")
        # mesma empresa e mesmo trimestre fiscal em duas datas futuras (remarcação em dia que falhou): fica a mais
        # recente coleta
        fut = cal["data"] > hoje.isoformat()
        f = cal[fut].sort_values("coletado_em").drop_duplicates(["ticker", "trimestre_fiscal"], keep="last")
        cal = pd.concat([cal[~fut], f]).sort_values(["data", "ticker"]).reset_index(drop=True)
        DADOS.mkdir(exist_ok=True)
        cal.to_parquet(ARQ_CAL, index=False, compression="zstd")
    return {"calendario_ok": True, "dias_lidos": len(ok), "dias_falha": len(falhas),
            "falhas": falhas[:30], "linhas": int(len(cal)),
            "primeira_data": str(cal["data"].min()) if len(cal) else None,
            "ultima_data": str(cal["data"].max()) if len(cal) else None}


def gerar_entregas() -> dict:
    arq = DADOS / "eua_trimestral.parquet"
    if not arq.exists():
        return {"entregas_ok": False}
    cols = ["cnpj", "ticker", "empresa", "setor", "tipo_especial", "dt_fim", "trimestre", "exercicio",
            "dt_arquivamento"]
    t = pd.read_parquet(arq, columns=cols)
    t = t[t["dt_arquivamento"].notna() & (t["dt_fim"] >= "2022-01-01")].copy()
    t["documento"] = t["trimestre"].map(lambda q: "10-K" if q == 4 else "10-Q")
    t = t.drop_duplicates(["cnpj", "dt_fim"], keep="last")
    t.to_parquet(ARQ_ENT, index=False, compression="zstd")
    print(f"entregas: {len(t)} linhas, {t['cnpj'].nunique()} empresas")
    return {"entregas_ok": True, "entregas_linhas": int(len(t))}


def main() -> int:
    meta = {"gerado_em": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    try:
        meta.update(gerar_entregas())
    except Exception as e:  # noqa: BLE001
        print("entregas falhou:", e)
        meta["entregas_ok"] = False
    try:
        meta.update(atualizar_calendario())
    except Exception as e:  # noqa: BLE001
        print("calendário falhou:", e)
        meta["calendario_ok"] = False
    antigo = json.loads(ARQ_META.read_text()) if ARQ_META.exists() else {}
    if not meta.get("calendario_ok") and antigo.get("ultima_coleta_ok"):
        meta["ultima_coleta_ok"] = antigo["ultima_coleta_ok"]
    elif meta.get("calendario_ok"):
        meta["ultima_coleta_ok"] = meta["gerado_em"]
    DADOS.mkdir(exist_ok=True)
    ARQ_META.write_text(json.dumps(meta, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
