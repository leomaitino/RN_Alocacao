"""
baixar_benchmarks.py
====================
Baixa séries históricas do IHFA e IMA-B da API pública da ANBIMA e
atualiza data/benchmarks.json.

Uso:
    python baixar_benchmarks.py                      # busca tudo que faltar
    python baixar_benchmarks.py --anos 5             # limita histórico a 5 anos
    python baixar_benchmarks.py --ihfa ihfa.csv      # usa CSV local para IHFA
    python baixar_benchmarks.py --indice IHFA        # só atualiza IHFA
    python baixar_benchmarks.py --indice IMA-B       # só atualiza IMA-B

Como o IHFA funciona via API:
    A ANBIMA fornece resultados diários em:
    GET https://api.anbima.com.br/feed/precos-indices/v1/indices/resultados-ihfa-fechado?data=AAAA-MM-DD

    Não requer token para consultas públicas do feed aberto.
    O campo 'numero_indice' é o nível do índice (base histórica ANBIMA).
    Convertemos para retorno diário: ret_d = (nivel_d / nivel_{d-1}) - 1

Como o IMA-B funciona via API:
    GET https://api.anbima.com.br/feed/precos-indices/v1/indices/resultados-ima?data=AAAA-MM-DD
    Filtramos pelo campo 'indice' == 'IMA-B'.
"""

import os
import sys
import json
import time
import logging
import argparse
import io
from pathlib import Path
from datetime import date, timedelta, datetime

import requests
import pandas as pd
import numpy as np

# ── logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format='[%(asctime)s] %(message)s',
    datefmt='%H:%M:%S',
)
log = logging.getLogger(__name__)

# ── config ───────────────────────────────────────────────────────────────────
DATA_DIR        = Path('data')
BENCHMARKS_FILE = DATA_DIR / 'benchmarks.json'
CACHE_DIR       = DATA_DIR / 'cache_benchmarks'

ANBIMA_IHFA_URL = 'https://api.anbima.com.br/feed/precos-indices/v1/indices/resultados-ihfa-fechado'
ANBIMA_IMA_URL  = 'https://api.anbima.com.br/feed/precos-indices/v1/indices/resultados-ima'

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (compatible; AlphaDesk/1.0)',
    'Accept': 'application/json',
}

# Throttle: 1 req/segundo para não sobrecarregar a API
THROTTLE_S = 1.1


# ── utils ─────────────────────────────────────────────────────────────────────

def dias_uteis(inicio: date, fim: date) -> list[date]:
    """Retorna lista de dias úteis BR (exclui fins de semana; feriados nacionais fixos)."""
    feriados_fixos = {
        (1, 1), (4, 21), (5, 1), (9, 7), (10, 12),
        (11, 2), (11, 15), (11, 20), (12, 25),
    }
    dias = []
    d = inicio
    while d <= fim:
        if d.weekday() < 5 and (d.month, d.day) not in feriados_fixos:
            dias.append(d)
        d += timedelta(days=1)
    return dias


def carregar_benchmarks() -> dict:
    if BENCHMARKS_FILE.exists():
        try:
            return json.loads(BENCHMARKS_FILE.read_text(encoding='utf-8'))
        except Exception as e:
            log.warning(f'Erro ao ler benchmarks.json: {e}')
    return {}


def salvar_benchmarks(bm: dict):
    DATA_DIR.mkdir(exist_ok=True)
    BENCHMARKS_FILE.write_text(
        json.dumps(bm, ensure_ascii=False, indent=2),
        encoding='utf-8'
    )
    size_kb = BENCHMARKS_FILE.stat().st_size / 1024
    log.info(f'  ✓ benchmarks.json salvo ({size_kb:.0f} KB)')


def calcular_acumulado(retornos: pd.Series) -> pd.Series:
    """Série cumulativa base 100 a partir de retornos diários decimais."""
    return (1 + retornos).cumprod() * 100


def serie_para_dict(s: pd.Series) -> dict:
    """Converte pd.Series com DatetimeIndex para {AAAA-MM-DD: valor}."""
    return {
        str(k.date()): round(float(v), 8)
        for k, v in s.items()
        if pd.notna(v)
    }


def nivel_para_retornos(niveis: pd.Series) -> pd.Series:
    """Converte série de nível de índice para retornos diários."""
    niveis = niveis.sort_index().dropna()
    retornos = niveis.pct_change().dropna()
    return retornos


def cache_path(indice: str, data: date) -> Path:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    return CACHE_DIR / f'{indice}_{data.isoformat()}.json'


def cache_get(indice: str, data: date):
    p = cache_path(indice, data)
    if p.exists():
        try:
            return json.loads(p.read_text())
        except Exception:
            pass
    return None


def cache_set(indice: str, data: date, valor):
    cache_path(indice, data).write_text(json.dumps(valor))


# ── download por data ─────────────────────────────────────────────────────────

def buscar_ihfa_dia(data: date) -> float | None:
    """Retorna numero_indice do IHFA para uma data. None se indisponível."""
    cached = cache_get('IHFA', data)
    if cached is not None:
        return cached  # pode ser False (=sem dados) ou float

    url = f'{ANBIMA_IHFA_URL}?data={data.isoformat()}'
    try:
        r = requests.get(url, headers=HEADERS, timeout=15)
        if r.status_code == 404 or r.status_code == 204:
            cache_set('IHFA', data, False)
            return None
        r.raise_for_status()
        data_json = r.json()

        # A resposta pode ser lista ou dict com campo de lista
        rows = data_json if isinstance(data_json, list) else data_json.get('data', [])
        if not rows:
            cache_set('IHFA', data, False)
            return None

        # Pega o primeiro registro (sem filtro de nome — o endpoint é exclusivo do IHFA)
        row = rows[0] if isinstance(rows, list) else rows
        nivel = float(row.get('numero_indice', 0) or 0)
        if nivel <= 0:
            cache_set('IHFA', data, False)
            return None

        cache_set('IHFA', data, nivel)
        return nivel
    except requests.exceptions.HTTPError as e:
        if e.response.status_code in (401, 403):
            log.warning(f'  IHFA {data}: acesso negado (endpoint pode exigir token)')
        return None
    except Exception:
        return None


def buscar_imab_dia(data: date) -> float | None:
    """Retorna numero_indice do IMA-B para uma data. None se indisponível."""
    cached = cache_get('IMA-B', data)
    if cached is not None:
        return cached

    url = f'{ANBIMA_IMA_URL}?data={data.isoformat()}'
    try:
        r = requests.get(url, headers=HEADERS, timeout=15)
        if r.status_code in (404, 204):
            cache_set('IMA-B', data, False)
            return None
        r.raise_for_status()
        data_json = r.json()

        rows = data_json if isinstance(data_json, list) else data_json.get('data', [])
        if not rows:
            cache_set('IMA-B', data, False)
            return None

        # Filtra pelo campo 'indice' == 'IMA-B'
        for row in rows:
            nome = (row.get('indice') or '').strip().upper()
            if nome == 'IMA-B':
                nivel = float(row.get('numero_indice', 0) or 0)
                if nivel > 0:
                    cache_set('IMA-B', data, nivel)
                    return nivel

        cache_set('IMA-B', data, False)
        return None
    except requests.exceptions.HTTPError as e:
        if e.response.status_code in (401, 403):
            log.warning(f'  IMA-B {data}: acesso negado (endpoint pode exigir token)')
        return None
    except Exception:
        return None


# ── download em lote ──────────────────────────────────────────────────────────

def baixar_serie_anbima(indice: str, datas: list[date], fn_dia) -> pd.Series:
    """
    Baixa série de níveis diários do índice para as datas fornecidas.
    Usa cache local para não repetir requests.
    Retorna pd.Series de retornos diários.
    """
    total = len(datas)
    niveis = {}
    sem_cache = [d for d in datas if cache_get(indice, d) is None]

    if sem_cache:
        log.info(f'  {indice}: {len(sem_cache)} datas sem cache, baixando...')
    else:
        log.info(f'  {indice}: tudo em cache local')

    for i, data in enumerate(sem_cache):
        nivel = fn_dia(data)
        if nivel:
            niveis[data] = nivel
        # Progresso a cada 50
        if (i + 1) % 50 == 0 or i == len(sem_cache) - 1:
            log.info(f'    {i+1}/{len(sem_cache)} ({indice})')
        time.sleep(THROTTLE_S)

    # Complementa com os que já estavam em cache
    for data in datas:
        if data not in niveis:
            cached = cache_get(indice, data)
            if cached and cached is not False:
                niveis[data] = cached

    if not niveis:
        return pd.Series(dtype=float)

    serie_niveis = pd.Series(niveis)
    serie_niveis.index = pd.to_datetime(serie_niveis.index)
    serie_niveis = serie_niveis.sort_index()

    retornos = nivel_para_retornos(serie_niveis)
    log.info(f'  ✓ {indice}: {len(retornos)} retornos diários ({retornos.index[0].date()} → {retornos.index[-1].date()})')
    return retornos


# ── IHFA via CSV local ─────────────────────────────────────────────────────────

def _parsear_csv_ihfa(conteudo_bytes: bytes) -> pd.Series:
    """Lê CSV do IHFA baixado manualmente da ANBIMA. Retorna série de retornos."""
    for encoding in ['latin-1', 'utf-8', 'utf-8-sig']:
        for sep in [';', ',', '\t']:
            try:
                df = pd.read_csv(
                    io.StringIO(conteudo_bytes.decode(encoding)),
                    sep=sep, dtype=str, header=0,
                )
                if df.shape[1] < 2:
                    continue
                df.columns = [str(c).strip() for c in df.columns]

                # Coluna de data
                data_col = next(
                    (c for c in df.columns if any(k in c.lower() for k in ['data', 'dt', 'date'])),
                    df.columns[0]
                )
                # Coluna de valor — prioriza "Número Índice" (nível absoluto)
                # sobre colunas de variação percentual
                val_col = next(
                    (c for c in df.columns if 'número' in c.lower() and 'índice' in c.lower()),
                    None
                ) or next(
                    (c for c in df.columns if 'numero' in c.lower() and 'indice' in c.lower()),
                    None
                ) or next(
                    (c for c in df.columns if any(k in c.lower() for k in
                        ['valor', 'ihfa', 'num_indice', 'numindice'])),
                    None
                ) or next(
                    (c for c in df.columns if any(k in c.lower() for k in
                        ['var', 'ret', '%'])),
                    df.columns[1]
                )

                df[data_col] = pd.to_datetime(df[data_col], dayfirst=True, errors='coerce')
                df[val_col]  = pd.to_numeric(
                    df[val_col].str.replace(',', '.', regex=False), errors='coerce'
                )
                df = df.dropna(subset=[data_col, val_col]).set_index(data_col)
                serie = df[val_col].sort_index()
                if len(serie) < 10:
                    continue

                # Decide se é nível ou retorno
                med = serie.abs().median()
                if med > 5:
                    # Valores absolutos grandes = nível do índice (ex: 1039, 4000)
                    serie = nivel_para_retornos(serie)
                elif med > 0.005:
                    # Valores em torno de 0.01-1.0 = variação em % (ex: 0.0456 = 0.0456%)
                    serie = serie / 100
                # else: já está em decimal (ex: CDI 0.00047/dia)

                serie = serie.rename('IHFA')
                log.info(f'  CSV IHFA lido: {len(serie)} dias ({encoding}, sep="{sep}")')
                return serie
            except Exception:
                continue
    log.warning('  Não foi possível parsear o CSV do IHFA.')
    return pd.Series(dtype=float)


def carregar_ihfa_csv(caminho: str) -> pd.Series:
    """Carrega série IHFA/IMA-B de CSV ou Excel (.xls/.xlsx)."""
    ext = os.path.splitext(caminho)[1].lower()
    if ext in ('.xls', '.xlsx', '.xlsm'):
        return _parsear_excel_anbima(caminho)
    with open(caminho, 'rb') as f:
        return _parsear_csv_ihfa(f.read())


def _parsear_excel_anbima(caminho: str) -> pd.Series:
    """Lê Excel (.xls/.xlsx) do ANBIMA Data. Retorna série de retornos diários."""
    try:
        df = pd.read_excel(caminho)
        df.columns = [str(c).strip() for c in df.columns]

        # Coluna de data
        data_col = next(
            (c for c in df.columns if any(k in c.lower() for k in ['data', 'dt', 'date'])),
            df.columns[0]
        )
        # Coluna de nível do índice
        val_col = next(
            (c for c in df.columns if 'número' in c.lower() and 'índice' in c.lower()),
            None
        ) or next(
            (c for c in df.columns if 'numero' in c.lower() and 'indice' in c.lower()),
            None
        ) or next(
            (c for c in df.columns if 'número índice' in c.lower()),
            df.columns[2] if len(df.columns) > 2 else df.columns[1]
        )

        df[data_col] = pd.to_datetime(df[data_col], errors='coerce')
        df[val_col] = pd.to_numeric(df[val_col], errors='coerce')
        df = df.dropna(subset=[data_col, val_col]).set_index(data_col)
        serie = df[val_col].sort_index()

        if len(serie) < 10:
            log.warning(f'  Excel com poucas linhas: {len(serie)}')
            return pd.Series(dtype=float)

        # Valores grandes = nível do índice → converter para retornos
        retornos = nivel_para_retornos(serie)
        log.info(f'  Excel lido: {len(retornos)} retornos ({retornos.index[0].date()} → {retornos.index[-1].date()})')
        return retornos
    except Exception as e:
        log.warning(f'  Erro ao ler Excel: {e}')
        return pd.Series(dtype=float)


# ── atualizar benchmark no dict ───────────────────────────────────────────────

def atualizar_benchmark(bm: dict, nome: str, serie_ret: pd.Series, tipo: str = 'indice'):
    """Merge da nova série no benchmark existente."""
    if serie_ret.empty:
        log.warning(f'  {nome}: série vazia, não atualizado.')
        return

    # Converte série existente
    existente_ret = {}
    if nome in bm and 'retornos_diarios' in bm[nome]:
        existente_ret = bm[nome]['retornos_diarios']

    # Merge: novos dados sobrescrevem se houver conflito de data
    novos = serie_para_dict(serie_ret)
    merged = {**existente_ret, **novos}

    # Reconstrói acumulado da série completa
    s = pd.Series({pd.Timestamp(k): v for k, v in sorted(merged.items())}, dtype=float)
    acum = calcular_acumulado(s)

    bm[nome] = {
        'nome': nome,
        'tipo': tipo,
        'retornos_diarios': {k: v for k, v in sorted(merged.items())},
        'acumulado': serie_para_dict(acum),
    }
    primeiro = min(merged.keys())
    ultimo   = max(merged.keys())
    log.info(f'  ✓ {nome} atualizado: {len(merged)} dias ({primeiro} → {ultimo})')


# ── main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description='Baixa IHFA e IMA-B e atualiza benchmarks.json')
    parser.add_argument('--anos',   type=int, default=5,    help='Anos de histórico a buscar (padrão: 5)')
    parser.add_argument('--ihfa',   default=None,           help='Caminho para CSV local do IHFA')
    parser.add_argument('--imab',   default=None,           help='Caminho para CSV local do IMA-B')
    parser.add_argument('--indice', default=None,           help='Atualizar só um índice: IHFA ou IMA-B')
    args = parser.parse_args()

    DATA_DIR.mkdir(exist_ok=True)
    CACHE_DIR.mkdir(parents=True, exist_ok=True)

    bm = carregar_benchmarks()
    log.info(f'benchmarks.json atual: {list(bm.keys())}')

    hoje  = date.today()
    inicio = date(hoje.year - args.anos, hoje.month, hoje.day)

    # Descobre a última data já salva para cada índice (só baixa o delta)
    def ultima_data_salva(nome: str) -> date | None:
        if nome not in bm or 'retornos_diarios' not in bm[nome]:
            return None
        datas = list(bm[nome]['retornos_diarios'].keys())
        if not datas:
            return None
        return date.fromisoformat(max(datas))

    fazer_ihfa = args.indice is None or args.indice.upper() == 'IHFA'
    fazer_imab = args.indice is None or args.indice.upper() in ('IMA-B', 'IMAB')

    # ── IHFA ──────────────────────────────────────────────────────────────────
    if fazer_ihfa:
        log.info('\n[IHFA]')
        if args.ihfa and os.path.exists(args.ihfa):
            # CSV local — mais confiável para histórico completo
            log.info(f'  Carregando CSV local: {args.ihfa}')
            serie_ihfa = carregar_ihfa_csv(args.ihfa)
            atualizar_benchmark(bm, 'IHFA', serie_ihfa)
        else:
            # Tenta API ANBIMA
            ultima = ultima_data_salva('IHFA')
            data_inicio_ihfa = (ultima + timedelta(days=1)) if ultima else inicio
            if data_inicio_ihfa > hoje:
                log.info('  IHFA já está atualizado.')
            else:
                datas = dias_uteis(data_inicio_ihfa, hoje - timedelta(days=1))
                log.info(f'  Buscando {len(datas)} dias úteis via API ({data_inicio_ihfa} → {hoje - timedelta(days=1)})')
                serie_ihfa = baixar_serie_anbima('IHFA', datas, buscar_ihfa_dia)
                if serie_ihfa.empty:
                    log.warning(
                        '\n  ⚠ API ANBIMA não retornou dados para o IHFA.\n'
                        '  Para obter o histórico completo, baixe o CSV manualmente:\n'
                        '    1. Acesse: https://www.anbima.com.br/pt_br/informar/precos-e-indices/indices/ihfa.htm\n'
                        '    2. Clique em "Download" > "Série Histórica"\n'
                        '    3. Salve o arquivo (ex: ihfa.csv) na pasta do projeto\n'
                        '    4. Rode: python baixar_benchmarks.py --ihfa ihfa.csv\n'
                    )
                else:
                    atualizar_benchmark(bm, 'IHFA', serie_ihfa)

    # ── IMA-B ─────────────────────────────────────────────────────────────────
    if fazer_imab:
        log.info('\n[IMA-B]')
        if args.imab and os.path.exists(args.imab):
            # CSV local — mesmo formato do IHFA
            log.info(f'  Carregando CSV local: {args.imab}')
            serie_imab = carregar_ihfa_csv(args.imab)  # mesmo parser, mesmo formato ANBIMA
            atualizar_benchmark(bm, 'IMA-B', serie_imab)
        else:
            ultima = ultima_data_salva('IMA-B')
            data_inicio_imab = (ultima + timedelta(days=1)) if ultima else inicio
            if data_inicio_imab > hoje:
                log.info('  IMA-B já está atualizado.')
            else:
                datas = dias_uteis(data_inicio_imab, hoje - timedelta(days=1))
                log.info(f'  Buscando {len(datas)} dias úteis via API ({data_inicio_imab} → {hoje - timedelta(days=1)})')
                serie_imab = baixar_serie_anbima('IMA-B', datas, buscar_imab_dia)
                if serie_imab.empty:
                    log.warning(
                        '\n  ⚠ API ANBIMA não retornou dados para o IMA-B.\n'
                        '  Baixe o CSV manualmente na ANBIMA e rode:\n'
                        '    python baixar_benchmarks.py --imab imab.csv\n'
                    )
                else:
                    atualizar_benchmark(bm, 'IMA-B', serie_imab)

    # ── Salva ──────────────────────────────────────────────────────────────────
    log.info('\n[Salvando]')
    salvar_benchmarks(bm)

    log.info('\n✓ Pronto!')
    log.info(f'  Índices em benchmarks.json: {list(bm.keys())}')
    for nome, dados in bm.items():
        n = len(dados.get('retornos_diarios', {}))
        log.info(f'    {nome}: {n} dias')


if __name__ == '__main__':
    main()
