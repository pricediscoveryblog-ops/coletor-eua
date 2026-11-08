"""
Sonda de fontes gratuitas de preço (EUA) rodando DE DENTRO do GitHub Actions.
Objetivo: descobrir empiricamente qual fonte não é bloqueada nos IPs do Actions antes de construir os múltiplos.
Grava dados/sonda_precos.json. Não interrompe o workflow se falhar.
"""
import datetime as dt
import io
import json
import time
from pathlib import Path

import pandas as pd
import requests

TICKERS = ["AAPL", "MSFT", "WMT", "KO", "DAL", "GOOGL", "GOOG", "BRK-B", "JPM", "XOM",
           "MELI", "PBR", "CAT", "MMM", "T", "F", "GE", "IBM", "INTC", "NVDA"]
saida = {"executado_em": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"), "fontes": {}}


def yfinance_teste():
    import yfinance as yf
    t0 = time.time()
    df = yf.download(TICKERS, period="10d", interval="1d", auto_adjust=False, progress=False, threads=False)
    fech = df["Close"] if "Close" in df else pd.DataFrame()
    ok = [t for t in TICKERS if t in fech and fech[t].notna().any()]
    return {"ok": len(ok), "total": len(TICKERS), "segundos": round(time.time() - t0, 1),
            "faltaram": [t for t in TICKERS if t not in ok],
            "exemplo": {t: round(float(fech[t].dropna().iloc[-1]), 2) for t in ok[:3]}}


def stooq_teste():
    t0, ok, erros = time.time(), [], {}
    for t in TICKERS[:8]:
        url = f"https://stooq.com/q/d/l/?s={t.lower().replace('-', '.')}.us&i=d"
        try:
            r = requests.get(url, timeout=30, headers={"User-Agent": "Mozilla/5.0"})
            if r.ok and r.text.startswith("Date"):
                d = pd.read_csv(io.StringIO(r.text))
                if len(d):
                    ok.append(t)
                    continue
            erros[t] = f"HTTP {r.status_code}: {r.text[:60]!r}"
        except Exception as e:
            erros[t] = str(e)[:80]
        time.sleep(1)
    return {"ok": len(ok), "total": 8, "segundos": round(time.time() - t0, 1), "erros": erros}


for nome, fn in (("yfinance", yfinance_teste), ("stooq", stooq_teste)):
    try:
        saida["fontes"][nome] = fn()
    except Exception as e:
        saida["fontes"][nome] = {"erro": f"{type(e).__name__}: {str(e)[:200]}"}

Path("dados").mkdir(exist_ok=True)
Path("dados/sonda_precos.json").write_text(json.dumps(saida, indent=2, ensure_ascii=False))
print(json.dumps(saida, indent=2, ensure_ascii=False))
