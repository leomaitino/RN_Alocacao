"""
atualizar_comparador.py
=======================
Atualiza benchmarks.json e mercado.json da pasta comparador.

Fontes:
  - CDI          : Banco Central (SGS série 12)
  - IMA-B        : ANBIMA (API pública)
  - IHFA         : ANBIMA (API pública)
  - IPCA         : Banco Central (SGS série 433) — para calcular IPCA+spreads
  - IBOV, IFIX   : Yahoo Finance (yfinance)
  - Dólar        : Yahoo Finance (BRL=X)
  - S&P 500      : Yahoo Finance (^GSPC)
  - Nasdaq       : Yahoo Finance (^IXIC)
  - Small Caps   : Yahoo Finance (^SMLLBVSP)
  - Ouro         : Yahoo Finance (GC=F)
  - Bolsa Mundo  : Yahoo Finance (ACWI)

Uso:
  pip install requests yfinance pandas
  python atualizar_comparador.py

  # Para forçar atualização desde uma data específica:
  python atualizar_comparador.py --desde 2026-01-01
"""

import json
import math
import argparse
import requests
import pandas as pd
from datetime import datetime, timedelta
from pathlib import Path

# ── Configuração ───────────────────────────────────────────────────────────────
SCRIPT_DIR    = Path(__file__).parent
BENCHMARKS    = SCRIPT_DIR / "benchmarks.json"
MERCADO       = SCRIPT_DIR / "mercado.json"
HOJE          = datetime.today().strftime("%Y-%m-%d")

# ── Helpers ────────────────────────────────────────────────────────────────────
def carregar_json(path):
    if path.exists():
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    return {}

def salvar_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print(f"  ✓ Salvo: {path.name}")

def ultima_data(retornos_diarios):
    if not retornos_diarios:
        return "2020-01-01"
    return sorted(retornos_diarios.keys())[-1]

def proximo_dia_util(data_str):
    d = datetime.strptime(data_str, "%Y-%m-%d") + timedelta(days=1)
    while d.weekday() >= 5:  # pula fim de semana
        d += timedelta(days=1)
    return d.strftime("%Y-%m-%d")

def fmt_data_bcb(d):
    return datetime.strptime(d, "%Y-%m-%d").strftime("%d/%m/%Y")

# ── BANCO CENTRAL (SGS) ───────────────────────────────────────────────────────
def buscar_bcb(serie, data_ini, data_fim=HOJE):
    """Busca série temporal do Banco Central (SGS)."""
    url = (
        f"https://api.bcb.gov.br/dados/serie/bcdata.sgs.{serie}/dados"
        f"?formato=json&dataInicial={fmt_data_bcb(data_ini)}&dataFinal={fmt_data_bcb(data_fim)}"
    )
    try:
        r = requests.get(url, timeout=30)
        r.raise_for_status()
        dados = r.json()
        result = {}
        for item in dados:
            d = datetime.strptime(item["data"], "%d/%m/%Y").strftime("%Y-%m-%d")
            v = float(item["valor"]) / 100  # BCB retorna % — converte para decimal
            result[d] = v
        return result
    except Exception as e:
        print(f"  ✗ Erro BCB série {serie}: {e}")
        return {}

# ── ANBIMA ────────────────────────────────────────────────────────────────────
def buscar_imab(data_ini, data_fim=HOJE):
    """Busca IMA-B via ANBIMA — retornos diários."""
    url = "https://www.anbima.com.br/informacoes/ima/ima-resultados.asp"
    # ANBIMA não tem API pública direta — usa arquivo histórico
    # Alternativa: série do BCB não existe para IMA-B
    # Melhor fonte: Yahoo Finance não tem IMA-B
    # Usar ANBIMA download direto
    try:
        # Tentativa 1: API ANBIMA (requer token para alguns endpoints)
        headers = {"User-Agent": "Mozilla/5.0"}
        params = {
            "Idioma": "PT",
            "Dt_Ref_Ini": data_ini.replace("-", "/"),
            "Dt_Ref_Fim": data_fim.replace("-", "/"),
            "indice": "IMA-B",
        }
        # ANBIMA publica dados em tabela — parsing simples
        r = requests.get(
            "https://www.anbima.com.br/informacoes/ima/ima-resultados.asp",
            params=params, headers=headers, timeout=30
        )
        if r.status_code == 200:
            df = pd.read_html(r.text)[0]
            # Tenta extrair retorno diário
            print(f"  ℹ ANBIMA IMA-B: {len(df)} linhas")
            return {}
    except Exception as e:
        print(f"  ✗ Erro ANBIMA IMA-B: {e}")
    return {}

def buscar_ihfa(data_ini, data_fim=HOJE):
    """IHFA via ANBIMA."""
    try:
        headers = {"User-Agent": "Mozilla/5.0"}
        r = requests.get(
            "https://www.anbima.com.br/informacoes/fundos/fundo-dia.asp",
            headers=headers, timeout=30
        )
        # ANBIMA requer autenticação para dados históricos
        print("  ℹ IHFA: ANBIMA requer download manual do histórico")
        return {}
    except Exception as e:
        print(f"  ✗ Erro ANBIMA IHFA: {e}")
    return {}

# ── YAHOO FINANCE ─────────────────────────────────────────────────────────────
def buscar_yahoo(ticker, nome, data_ini, data_fim=HOJE):
    """Busca retornos diários via yfinance."""
    try:
        import yfinance as yf
        di = (datetime.strptime(data_ini, "%Y-%m-%d") - timedelta(days=5)).strftime("%Y-%m-%d")
        df = yf.download(ticker, start=di, end=data_fim, progress=False, auto_adjust=True)
        if df.empty:
            print(f"  ✗ {nome} ({ticker}): sem dados")
            return {}

        # Fix MultiIndex — yfinance >= 0.2.x retorna MultiIndex em algumas versões
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)

        closes = df["Close"].dropna()

        # Garante que é Series 1D
        if isinstance(closes, pd.DataFrame):
            closes = closes.iloc[:, 0]

        rets = closes.pct_change().dropna()
        result = {}
        for date, ret in rets.items():
            d = date.strftime("%Y-%m-%d") if hasattr(date, 'strftime') else str(date)[:10]
            if d >= data_ini:
                result[d] = float(ret)
        print(f"  ✓ {nome} ({ticker}): {len(result)} novos pontos")
        return result
    except ImportError:
        print("  ✗ yfinance não instalado: pip install yfinance")
        return {}
    except Exception as e:
        print(f"  ✗ Erro Yahoo {ticker}: {e}")
        return {}

# ── IPCA + SPREAD (sintético) ─────────────────────────────────────────────────
def calcular_ipca_spread(ipca_mensal, spread_anual, datas):
    """
    Constrói série diária de IPCA+spread.
    ipca_mensal: dict {YYYY-MM-DD: valor_mensal_decimal}
    spread_anual: float (ex: 0.05 para 5%)
    datas: lista de datas para as quais calcular
    """
    # Converte IPCA mensal para diário usando interpolação linear
    # IPCA mensal → taxa diária = (1 + ipca_mes)^(1/dias_no_mes) - 1
    result = {}
    spread_diario = math.pow(1 + spread_anual, 1/252) - 1

    for d in sorted(datas):
        # Pega o IPCA do mês
        mes = d[:7] + "-01"  # YYYY-MM-01
        # Procura o IPCA mais próximo
        ipca_val = None
        for k in sorted(ipca_mensal.keys(), reverse=True):
            if k <= d:
                ipca_val = ipca_mensal[k]
                break
        if ipca_val is None:
            ipca_val = 0.0

        # IPCA diário: distribui o IPCA mensal em dias úteis do mês
        ipca_diario = math.pow(1 + ipca_val, 1/21) - 1  # ~21 dias úteis/mês
        # IPCA+spread = composição diária
        ret_diario = (1 + ipca_diario) * (1 + spread_diario) - 1
        result[d] = ret_diario

    return result

# ── UPDATE BENCHMARKS ─────────────────────────────────────────────────────────
def atualizar_benchmarks(desde=None):
    print("\n📊 Atualizando benchmarks.json...")
    dados = carregar_json(BENCHMARKS)

    # ── CDI ──
    print("\n  → CDI (Banco Central SGS 12)")
    if "CDI" not in dados:
        dados["CDI"] = {"nome": "CDI", "tipo": "taxa", "retornos_diarios": {}, "acumulado": {}}
    ini = desde or proximo_dia_util(ultima_data(dados["CDI"]["retornos_diarios"]))
    novos = buscar_bcb(12, ini)  # série 12 = CDI diário
    if novos:
        dados["CDI"]["retornos_diarios"].update(novos)
        print(f"  ✓ CDI: +{len(novos)} pontos (até {max(novos.keys())})")
    else:
        print("  ℹ CDI: sem novos dados ou já atualizado")

    # ── IPCA (para construir spreads) ──
    print("\n  → IPCA (Banco Central SGS 433)")
    ipca_ini = desde or "2021-01-01"
    ipca_mensal = buscar_bcb(433, ipca_ini)  # série 433 = IPCA mensal
    if ipca_mensal:
        print(f"  ✓ IPCA: {len(ipca_mensal)} pontos mensais")

    # ── IPCA + spreads ──
    cdi_datas = sorted(dados["CDI"]["retornos_diarios"].keys())
    for spread_nome, spread_val in [("IPCA+3.5%", 0.035), ("IPCA+5%", 0.05), ("IPCA+6%", 0.06)]:
        print(f"\n  → {spread_nome} (sintético)")
        if spread_nome not in dados:
            dados[spread_nome] = {"nome": spread_nome, "tipo": "ipca_spread", "retornos_diarios": {}, "acumulado": {}}
        ini_spread = desde or proximo_dia_util(ultima_data(dados[spread_nome]["retornos_diarios"]))
        novas_datas = [d for d in cdi_datas if d >= ini_spread]
        if novas_datas and ipca_mensal:
            novos_spread = calcular_ipca_spread(ipca_mensal, spread_val, novas_datas)
            dados[spread_nome]["retornos_diarios"].update(novos_spread)
            print(f"  ✓ {spread_nome}: +{len(novos_spread)} pontos")
        else:
            print(f"  ℹ {spread_nome}: sem dados novos ou IPCA não disponível")

    # ── IMA-B ──
    print("\n  → IMA-B (ANBIMA)")
    print("  ⚠ IMA-B requer download manual da ANBIMA:")
    print("    1. Acesse: https://www.anbima.com.br/pt_br/informar/ima.htm")
    print("    2. Baixe o histórico do IMA-B")
    print("    3. Execute: python atualizar_comparador.py --imab arquivo_imab.xlsx")

    # ── IHFA ──
    print("\n  → IHFA (ANBIMA)")
    print("  ⚠ IHFA requer download manual da ANBIMA:")
    print("    1. Acesse: https://www.anbima.com.br/pt_br/informar/fundos-de-investimento.htm")
    print("    2. Baixe o histórico do IHFA")
    print("    3. Execute: python atualizar_comparador.py --ihfa arquivo_ihfa.xlsx")

    salvar_json(BENCHMARKS, dados)

# ── UPDATE MERCADO ────────────────────────────────────────────────────────────
def atualizar_mercado(desde=None):
    print("\n📈 Atualizando mercado.json...")
    dados = carregar_json(MERCADO)

    TICKERS = {
        "IBOV":             "^BVSP",
        "Dólar":            "USDBRL=X",
        "Small Caps Brasil":"SMAL11.SA",
        "IFIX":             "XFIX11.SA",
        "S&P 500":          "^GSPC",
        "Ouro":             "GC=F",
        "Bolsa Mundo":      "ACWI",
        "Nasdaq":           "^IXIC",
    }

    for nome, ticker in TICKERS.items():
        print(f"\n  → {nome} ({ticker})")
        if nome not in dados:
            dados[nome] = {"nome": nome, "tipo": "mercado", "retornos_diarios": {}}
        ini = desde or proximo_dia_util(ultima_data(dados[nome]["retornos_diarios"]))
        novos = buscar_yahoo(ticker, nome, ini)
        if novos:
            dados[nome]["retornos_diarios"].update(novos)

    # CDI e IMA-B no mercado.json — copia do benchmarks.json
    print("\n  → Sincronizando CDI/IMA-B do benchmarks.json...")
    bench = carregar_json(BENCHMARKS)
    for k in ["CDI", "IMA-B", "IHFA"]:
        if k in bench and k in dados:
            # Adiciona apenas datas novas
            novos_bench = {d: v for d, v in bench[k]["retornos_diarios"].items()
                          if d not in dados[k]["retornos_diarios"]}
            if novos_bench:
                dados[k]["retornos_diarios"].update(novos_bench)
                print(f"  ✓ {k}: +{len(novos_bench)} pontos sincronizados")

    salvar_json(MERCADO, dados)

# ── IMPORTAR ANBIMA XLSX ──────────────────────────────────────────────────────
def importar_imab_xlsx(arquivo):
    """Importa histórico do IMA-B de arquivo Excel da ANBIMA."""
    print(f"\n📥 Importando IMA-B de {arquivo}...")
    try:
        df = pd.read_excel(arquivo, skiprows=1)
        print(f"  Colunas encontradas: {list(df.columns)}")
        # ANBIMA geralmente tem: Data, Número Índice, Retorno Diário, ...
        # Tenta identificar colunas automaticamente
        col_data = None
        col_ret  = None
        for col in df.columns:
            cs = str(col).lower()
            if 'data' in cs or 'date' in cs: col_data = col
            if 'retorno' in cs and 'dia' in cs: col_ret = col
        if not col_data or not col_ret:
            print(f"  ✗ Não foi possível identificar colunas. Colunas disponíveis: {list(df.columns)}")
            print("  Edite o script e ajuste col_data e col_ret manualmente.")
            return
        df = df[[col_data, col_ret]].dropna()
        dados = carregar_json(BENCHMARKS)
        if "IMA-B" not in dados:
            dados["IMA-B"] = {"nome": "IMA-B", "tipo": "indice", "retornos_diarios": {}, "acumulado": {}}
        novos = 0
        for _, row in df.iterrows():
            try:
                d = pd.to_datetime(row[col_data]).strftime("%Y-%m-%d")
                v = float(row[col_ret]) / 100 if float(row[col_ret]) > 0.1 else float(row[col_ret])
                if d not in dados["IMA-B"]["retornos_diarios"]:
                    dados["IMA-B"]["retornos_diarios"][d] = v
                    novos += 1
            except: pass
        salvar_json(BENCHMARKS, dados)
        print(f"  ✓ IMA-B: +{novos} novos pontos importados")
    except Exception as e:
        print(f"  ✗ Erro ao importar: {e}")

def importar_ihfa_xlsx(arquivo):
    """Importa histórico do IHFA de arquivo Excel da ANBIMA."""
    print(f"\n📥 Importando IHFA de {arquivo}...")
    try:
        df = pd.read_excel(arquivo, skiprows=1)
        print(f"  Colunas encontradas: {list(df.columns)}")
        col_data = None
        col_ret  = None
        for col in df.columns:
            cs = str(col).lower()
            if 'data' in cs or 'date' in cs: col_data = col
            if 'retorno' in cs and 'dia' in cs: col_ret = col
        if not col_data or not col_ret:
            print(f"  ✗ Colunas não identificadas: {list(df.columns)}")
            return
        df = df[[col_data, col_ret]].dropna()
        dados = carregar_json(BENCHMARKS)
        if "IHFA" not in dados:
            dados["IHFA"] = {"nome": "IHFA", "tipo": "indice", "retornos_diarios": {}, "acumulado": {}}
        novos = 0
        for _, row in df.iterrows():
            try:
                d = pd.to_datetime(row[col_data]).strftime("%Y-%m-%d")
                v = float(row[col_ret]) / 100 if float(row[col_ret]) > 0.1 else float(row[col_ret])
                if d not in dados["IHFA"]["retornos_diarios"]:
                    dados["IHFA"]["retornos_diarios"][d] = v
                    novos += 1
            except: pass
        salvar_json(BENCHMARKS, dados)
        print(f"  ✓ IHFA: +{novos} novos pontos importados")
    except Exception as e:
        print(f"  ✗ Erro ao importar: {e}")

# ── MAIN ──────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Atualiza dados do Comparador de Carteiras")
    parser.add_argument("--desde",  help="Forçar atualização desde data (YYYY-MM-DD)", default=None)
    parser.add_argument("--imab",   help="Arquivo Excel IMA-B da ANBIMA",              default=None)
    parser.add_argument("--ihfa",   help="Arquivo Excel IHFA da ANBIMA",               default=None)
    parser.add_argument("--apenas-mercado", action="store_true", help="Atualiza só mercado.json")
    parser.add_argument("--apenas-bench",   action="store_true", help="Atualiza só benchmarks.json")
    args = parser.parse_args()

    print(f"🗓  Atualização de dados — {HOJE}")
    print(f"📁 Pasta: {SCRIPT_DIR}")

    if args.imab:
        importar_imab_xlsx(args.imab)
    elif args.ihfa:
        importar_ihfa_xlsx(args.ihfa)
    elif args.apenas_mercado:
        atualizar_mercado(args.desde)
    elif args.apenas_bench:
        atualizar_benchmarks(args.desde)
    else:
        atualizar_benchmarks(args.desde)
        atualizar_mercado(args.desde)

    print("\n✅ Concluído! Copie benchmarks.json e mercado.json para a pasta comparador/ no GitHub.")
    print("   Depois suba os arquivos e faça o deploy no Render.")
