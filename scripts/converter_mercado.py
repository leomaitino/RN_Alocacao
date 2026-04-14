"""
converter_mercado.py
====================
Lê mercado_series.xlsx e gera data/mercado.json.

Uso:
    python converter_mercado.py
    python converter_mercado.py --arquivo mercado_series.xlsx
"""

import json
import argparse
import logging
from pathlib import Path
import pandas as pd

logging.basicConfig(level=logging.INFO, format='[%(asctime)s] %(message)s', datefmt='%H:%M:%S')
log = logging.getLogger(__name__)

DATA_DIR    = Path('data')
OUTPUT_FILE = DATA_DIR / 'mercado.json'


def converter(arquivo: str):
    DATA_DIR.mkdir(exist_ok=True)

    log.info(f'Lendo {arquivo}...')
    try:
        df = pd.read_excel(arquivo, sheet_name='Series', header=3, dtype=str)
    except Exception as e:
        log.error(f'Erro ao ler planilha: {e}')
        return

    # Primeira coluna = data
    date_col = df.columns[0]
    df[date_col] = pd.to_datetime(df[date_col], dayfirst=True, errors='coerce')
    df = df.dropna(subset=[date_col])
    df = df.set_index(date_col).sort_index()

    # Carrega mercado.json existente para preservar séries anteriores
    mercado = {}
    if OUTPUT_FILE.exists():
        try:
            mercado = json.loads(OUTPUT_FILE.read_text(encoding='utf-8'))
            log.info(f'mercado.json existente: {list(mercado.keys())}')
        except Exception:
            pass

    for col in df.columns:
        nome = str(col).strip()
        if not nome or nome.startswith('←') or nome.startswith('='):
            continue

        serie = pd.to_numeric(
            df[col].str.replace(',', '.', regex=False).str.replace(' ', '', regex=False),
            errors='coerce'
        ).dropna()

        if len(serie) < 2:
            log.warning(f'  {nome}: dados insuficientes ({len(serie)} linhas), ignorado')
            continue

        # Detecta se é nível ou retorno
        med = serie.abs().median()
        if med > 5:
            # Nível absoluto — calcula retornos via pct_change
            retornos = serie.pct_change().dropna()
            log.info(f'  {nome}: nível detectado (mediana={med:.1f}), {len(retornos)} retornos')
        elif med > 0.005:
            # Variação em % (ex: 0.05 = 0.05%)
            retornos = serie / 100
            log.info(f'  {nome}: variação % detectada, dividindo por 100, {len(retornos)} retornos')
        else:
            # Já em decimal
            retornos = serie
            log.info(f'  {nome}: retorno decimal detectado, {len(retornos)} retornos')

        rd = {str(k.date()): round(float(v), 8) for k, v in retornos.items() if pd.notna(v)}
        if not rd:
            continue

        # Acumulado base 100
        val = 100.0
        acum = {}
        for d, r in sorted(rd.items()):
            val *= (1 + r)
            acum[d] = round(val, 4)

        mercado[nome] = {
            'nome': nome,
            'tipo': 'mercado',
            'retornos_diarios': dict(sorted(rd.items())),
            'acumulado': dict(sorted(acum.items())),
        }
        log.info(f'  ✓ {nome}: {len(rd)} dias ({min(rd.keys())} → {max(rd.keys())})')

    OUTPUT_FILE.write_text(json.dumps(mercado, ensure_ascii=False, indent=2), encoding='utf-8')
    size_kb = OUTPUT_FILE.stat().st_size / 1024
    log.info(f'\n✓ mercado.json salvo ({size_kb:.0f} KB)')
    log.info(f'  Índices: {list(mercado.keys())}')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--arquivo', default='mercado_series.xlsx',
                        help='Caminho para o Excel de séries (padrão: mercado_series.xlsx)')
    args = parser.parse_args()
    converter(args.arquivo)


if __name__ == '__main__':
    main()
