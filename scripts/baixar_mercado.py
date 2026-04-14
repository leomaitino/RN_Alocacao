"""
baixar_mercado.py
=================
Baixa séries históricas de índices de mercado via yfinance e salva em data/mercado.json.

Uso:
    python baixar_mercado.py              # baixa todos os índices
    python baixar_mercado.py --anos 10   # histórico de 10 anos

Instalar dependência:
    pip install yfinance
"""

import json
import logging
import argparse
from pathlib import Path
from datetime import date, timedelta

logging.basicConfig(level=logging.INFO, format='[%(asctime)s] %(message)s', datefmt='%H:%M:%S')
log = logging.getLogger(__name__)

DATA_DIR    = Path('data')
OUTPUT_FILE = DATA_DIR / 'mercado.json'

# Tickers disponíveis
TICKERS = {
    'IBOV':   '^BVSP',
    'S&P500': '^GSPC',
    'NASDAQ': '^IXIC',
    'ACWI':   'ACWI',
    'Ouro':   'GC=F',
    'Dólar':  'BRL=X',
    'SMLL':   'SMAL11.SA',
    'IFIX':   'IFIX11.SA',
}

def baixar_serie(ticker_symbol: str, nome: str, anos: int) -> dict | None:
    try:
        import yfinance as yf
    except ImportError:
        log.error('yfinance não instalado. Rode: pip install yfinance')
        return None

    inicio = date.today() - timedelta(days=anos * 365)
    log.info(f'  Baixando {nome} ({ticker_symbol})...')
    try:
        tk = yf.Ticker(ticker_symbol)
        hist = tk.history(start=inicio.isoformat(), auto_adjust=True)
        if hist.empty:
            log.warning(f'  {nome}: sem dados')
            return None

        # Retornos diários a partir do preço de fechamento
        closes = hist['Close'].dropna()
        retornos = closes.pct_change().dropna()

        rd = {str(k.date()): round(float(v), 8) for k, v in retornos.items()}
        acum_vals = {}
        val = 100.0
        for k, v in sorted(rd.items()):
            val *= (1 + v)
            acum_vals[k] = round(val, 4)

        log.info(f'  ✓ {nome}: {len(rd)} dias ({min(rd.keys())} → {max(rd.keys())})')
        return {
            'nome': nome,
            'ticker': ticker_symbol,
            'tipo': 'mercado',
            'retornos_diarios': dict(sorted(rd.items())),
            'acumulado': dict(sorted(acum_vals.items())),
        }
    except Exception as e:
        log.warning(f'  {nome}: erro — {e}')
        return None


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--anos', type=int, default=10, help='Anos de histórico (padrão: 10)')
    args = parser.parse_args()

    DATA_DIR.mkdir(exist_ok=True)

    # Carrega existente
    mercado = {}
    if OUTPUT_FILE.exists():
        try:
            mercado = json.loads(OUTPUT_FILE.read_text(encoding='utf-8'))
            log.info(f'mercado.json existente: {list(mercado.keys())}')
        except Exception:
            pass

    for nome, ticker in TICKERS.items():
        dados = baixar_serie(ticker, nome, args.anos)
        if dados:
            mercado[nome] = dados

    OUTPUT_FILE.write_text(json.dumps(mercado, ensure_ascii=False, indent=2), encoding='utf-8')
    size_kb = OUTPUT_FILE.stat().st_size / 1024
    log.info(f'\n✓ mercado.json salvo ({size_kb:.0f} KB)')
    log.info(f'  Índices: {list(mercado.keys())}')


if __name__ == '__main__':
    main()
