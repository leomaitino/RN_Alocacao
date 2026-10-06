#!/usr/bin/env python3
"""
================================================================================
ALPHA DESK — Pipeline de Fundos Listados (FI-Infra, FII de papel, Fiagro)
================================================================================

Monta os JSONs do dashboard_listados.html a partir de:

  1. data/listados_universo.json   → universo curado (ticker, CNPJ, tipo, gestor,
                                      status). Fonte de verdade editável.
  2. Yahoo Finance (yfinance)      → preço diário, volume e proventos (TICKER.SA)
  3. CVM — Informe Diário (555)    → cota patrimonial DIÁRIA, PL e cotistas dos
                                      FI-Infra (fundos de cotas listados)
  4. CVM — Informe Mensal FII      → VP/cota, PL, cotas emitidas, DY e
                                      composição ativo/passivo dos FIIs (e dos
                                      Fiagro-FII até ago/2025)
  5. CVM — Informe Mensal FIAGRO   → idem para Fiagros (dataset próprio desde
                                      mai/2025)
  6. CVM — CDA (composição)        → carteira dos FI-Infra com look-through
                                      FIC → master (debêntures: código, ISIN,
                                      vencimento, valor); posições confidenciais
                                      são sinalizadas
  7. ANBIMA — SND                  → código da debênture → emissor, CNPJ,
                                      indexador, cupom de emissão, 12.431
  8. data/listados_eventos_stress.json → eventos curados (emissor ou fundo)
                                      cruzados com a carteira

Outputs (data/):
  listados.json            → 1 registro por fundo com cadastro + métricas
  listados_series.json     → séries: preço, VP, P/VP, proventos
  listados_carteiras.json  → carteira look-through, indexadores, alertas
  meta_listados.json       → datas de referência e cobertura por fonte

Uso:
  python scripts/pipeline_listados.py                 # tudo
  python scripts/pipeline_listados.py --anos 3        # histórico CVM diário (padrão 3)
  python scripts/pipeline_listados.py --cda 202608    # força mês da CDA
  python scripts/pipeline_listados.py --sem-cda       # pula carteira
"""

import argparse
import hashlib
import io
import json
import logging
import math
import os
import re
import sys
import unicodedata
import zipfile
from datetime import date, datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import requests

sys.path.insert(0, str(Path(__file__).parent))
from pipeline_fundos import (  # noqa: E402  [SHARED-RF] helpers reutilizados
    NumpyEncoder, baixar_informes_cvm, _normalizar_cnpj, _to_serializable,
)

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s', datefmt='%H:%M:%S')
log = logging.getLogger('listados')

HOJE = date.today()
CVM_FII_URL    = "https://dados.cvm.gov.br/dados/FII/DOC/INF_MENSAL/DADOS/inf_mensal_fii_{ano}.zip"
CVM_FIAGRO_URL = "https://dados.cvm.gov.br/dados/FIAGRO/DOC/INF_MENSAL/DADOS/inf_mensal_fiagro_{aaaamm}.zip"
CVM_CDA_URL    = "https://dados.cvm.gov.br/dados/FI/DOC/CDA/DADOS/cda_fi_{aaaamm}.zip"
CVM_FIP_URL    = "https://dados.cvm.gov.br/dados/FIP/DOC/INF_QUADRIMESTRAL/DADOS/inf_quadrimestral_fip_{ano}.csv"
SND_URL = ("https://www.debentures.com.br/exploreosnd/consultaadados/emissoesdedebentures/"
           "caracteristicas_e.asp?op_exc=False&emissor=&isin=&ativo=&dt_ini=&dt_fim=&Submit.x=&Submit.y=")
UA = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AlphaDesk/1.0'}
FIAGRO_DATASET_INICIO = (2025, 5)   # primeiro mês publicado no dataset FIAGRO
DIAS_UTEIS_ANO = 252


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def norm_txt(s: str) -> str:
    s = unicodedata.normalize('NFKD', str(s or '')).encode('ascii', 'ignore').decode().upper()
    return re.sub(r'\s+', ' ', re.sub(r'[^A-Z0-9 ]', ' ', s)).strip()


def N(cnpj) -> str:
    return re.sub(r'\D', '', str(cnpj or ''))


def fnum(x):
    try:
        v = float(str(x).replace(',', '.'))
        return v if math.isfinite(v) else None
    except (TypeError, ValueError):
        return None


def r4(x, nd=4):
    return None if x is None or (isinstance(x, float) and not math.isfinite(x)) else round(float(x), nd)


def meses_atras(n: int) -> tuple[int, int]:
    y, m = HOJE.year, HOJE.month - n
    while m <= 0:
        m += 12; y -= 1
    return y, m


def baixar_arquivo(url: str, destino: Path, max_idade_dias: float | None = None, timeout=300) -> bool:
    """Baixa para o cache se não existir ou se estiver mais velho que max_idade_dias. Retorna True se há arquivo."""
    if destino.exists():
        idade = (datetime.now() - datetime.fromtimestamp(destino.stat().st_mtime)).days
        if max_idade_dias is None or idade < max_idade_dias:
            return True
    try:
        r = requests.get(url, headers=UA, timeout=timeout)
        if r.status_code != 200 or len(r.content) < 500:
            log.warning(f"  ✗ {url.split('/')[-1]}: HTTP {r.status_code}")
            return destino.exists()
        destino.parent.mkdir(parents=True, exist_ok=True)
        destino.write_bytes(r.content)
        log.info(f"  ✓ {destino.name} ({len(r.content)/1e6:.1f} MB)")
        return True
    except Exception as e:
        log.warning(f"  ✗ {url.split('/')[-1]}: {e}")
        return destino.exists()


def ler_csv_zip(zip_path: Path, nome_contem: str, **kw) -> pd.DataFrame:
    with zipfile.ZipFile(zip_path) as z:
        nome = next((n for n in z.namelist() if nome_contem in n), None)
        if not nome:
            return pd.DataFrame()
        return pd.read_csv(z.open(nome), sep=';', encoding='latin-1', dtype=str, low_memory=False, **kw)


# ---------------------------------------------------------------------------
# 1. Universo
# ---------------------------------------------------------------------------

def carregar_universo(path: Path) -> list[dict]:
    u = json.loads(path.read_text(encoding='utf-8'))
    fundos = [f for f in u['fundos'] if f.get('ticker') and f.get('cnpj')]
    log.info(f"Universo: {len(fundos)} fundos | painel {sum(1 for f in fundos if f.get('no_painel'))} | "
             f"tipos {pd.Series([f['tipo'] for f in fundos]).value_counts().to_dict()}")
    return fundos


# ---------------------------------------------------------------------------
# 2. Yahoo Finance — preços, volume, proventos
# ---------------------------------------------------------------------------

def corrigir_proventos_yahoo(ticker: str, divs: pd.Series) -> pd.Series:
    """
    O Yahoo publica alguns proventos com a casa decimal deslocada (caso real:
    AAZQ11 0,925 em jun/2026 quando o rendimento foi 0,0925; VCRI11 0,99 em
    nov/2025). Regra: valor > 6× a mediana dos 12 proventos vizinhos E valor/10
    dentro de ±60% dessa mediana → divide por 10 e loga [PROVENTO_FIX]. Valores
    grandes que não casam com /10 (ex.: amortização real) são mantidos.
    """
    if divs is None or len(divs) < 4:
        return divs
    divs = divs.sort_index().copy()
    vals = divs.values.astype(float)
    for i, v in enumerate(vals):
        viz = np.concatenate([vals[max(0, i - 6):i], vals[i + 1:i + 7]])
        viz = viz[viz > 0]
        if len(viz) < 3:
            continue
        med = float(np.median(viz))
        if v > 6 * med and abs(v / 10 - med) <= 0.6 * med:
            log.warning(f"  [PROVENTO_FIX] {ticker} {divs.index[i].date()}: {v:.4f} → {v/10:.4f} (mediana vizinha {med:.4f})")
            vals[i] = v / 10
    return pd.Series(vals, index=divs.index)


def baixar_yahoo(tickers: list[str], anos: int = 5) -> dict:
    import yfinance as yf
    syms = [t + '.SA' for t in tickers]
    log.info(f"Yahoo: baixando {len(syms)} tickers ({anos} anos, com proventos)...")
    raw = yf.download(syms, period=f'{anos}y', auto_adjust=False, actions=True,
                      progress=False, group_by='ticker', threads=True)
    out = {}
    for t, s in zip(tickers, syms):
        try:
            d = raw[s] if isinstance(raw.columns, pd.MultiIndex) else raw
            d = d.copy()
            d.index = pd.to_datetime(d.index).tz_localize(None) if getattr(d.index, 'tz', None) is not None else pd.to_datetime(d.index)
            px = d['Close'].dropna()
            if px.empty:
                log.warning(f"  {t}: sem preços no Yahoo"); continue
            vol_fin = (d['Close'] * d['Volume']).dropna()
            divs = d['Dividends'].dropna() if 'Dividends' in d.columns else pd.Series(dtype=float)
            divs = divs[divs > 0]
            divs = corrigir_proventos_yahoo(t, divs)
            out[t] = {'close': px, 'vol_fin': vol_fin, 'dividends': divs}
        except Exception as e:
            log.warning(f"  {t}: erro Yahoo: {e}")
    log.info(f"  → {len(out)} tickers com preço | última data: {max(v['close'].index.max() for v in out.values()).date() if out else '-'}")
    return out


# ---------------------------------------------------------------------------
# 3. CVM — informe diário (FI-Infra)
# ---------------------------------------------------------------------------

def baixar_cvm_diario(cnpjs: set[str], anos: int, cache_dir: Path) -> pd.DataFrame:
    if not cnpjs:
        return pd.DataFrame()
    # cache por conjunto de CNPJs: o parquet mensal é filtrado pelo universo → universo novo = cache novo
    h = hashlib.md5(','.join(sorted(N(c) for c in cnpjs)).encode()).hexdigest()[:8]
    pasta = cache_dir / f'inf_diario_{h}'
    log.info(f"CVM informe diário: {len(cnpjs)} FI-Infra, {anos*12} meses, cache {pasta.name}")
    df = baixar_informes_cvm(cnpjs, n_meses=anos * 12, pasta_cache=pasta)
    if df.empty:
        return df
    df = df[df['VL_QUOTA'] > 0]
    return df


# ---------------------------------------------------------------------------
# 4/5. CVM — informes mensais FII e FIAGRO
# ---------------------------------------------------------------------------

def baixar_cvm_fii(anos: int, cache_dir: Path) -> dict:
    """Retorna {'complemento': df, 'ativo_passivo': df, 'geral': df} concatenados dos últimos `anos` anos."""
    frames = {'complemento': [], 'ativo_passivo': [], 'geral': []}
    for ano in range(HOJE.year - anos, HOJE.year + 1):
        dest = cache_dir / f'inf_mensal_fii_{ano}.zip'
        ok = baixar_arquivo(CVM_FII_URL.format(ano=ano), dest, max_idade_dias=(1 if ano == HOJE.year else None))
        if not ok:
            continue
        for k in frames:
            d = ler_csv_zip(dest, k)
            if not d.empty:
                frames[k].append(d)
    out = {k: (pd.concat(v, ignore_index=True) if v else pd.DataFrame()) for k, v in frames.items()}
    for k, d in out.items():
        if not d.empty:
            d['N'] = d['CNPJ_Fundo_Classe'].map(N)
    log.info(f"CVM FII mensal: complemento {len(out['complemento'])} linhas | última ref "
             f"{out['complemento']['Data_Referencia'].max() if not out['complemento'].empty else '-'}")
    return out


def baixar_cvm_fiagro(cache_dir: Path) -> dict:
    frames_main, frames_sub = [], []
    y, m = FIAGRO_DATASET_INICIO
    meses = []
    while (y, m) <= (HOJE.year, HOJE.month):
        meses.append(f"{y}{m:02d}")
        m += 1
        if m > 12:
            m = 1; y += 1
    for i, aaaamm in enumerate(meses):
        dest = cache_dir / f'inf_mensal_fiagro_{aaaamm}.zip'
        recente = i >= len(meses) - 2
        ok = baixar_arquivo(CVM_FIAGRO_URL.format(aaaamm=aaaamm), dest, max_idade_dias=(1 if recente else None), timeout=120)
        if not ok:
            continue
        d = ler_csv_zip(dest, f'inf_mensal_fiagro_{aaaamm}')
        s = ler_csv_zip(dest, 'subclasse')
        if not d.empty:
            frames_main.append(d)
        if not s.empty:
            frames_sub.append(s)
    main = pd.concat(frames_main, ignore_index=True) if frames_main else pd.DataFrame()
    sub = pd.concat(frames_sub, ignore_index=True) if frames_sub else pd.DataFrame()
    for d in (main, sub):
        if not d.empty:
            d['N'] = d['CNPJ_Classe'].map(N)
    log.info(f"CVM FIAGRO mensal: {len(main)} linhas | última ref {main['Data_Referencia'].max() if not main.empty else '-'}")
    return {'main': main, 'sub': sub}


def baixar_cvm_fip(anos: int, cache_dir: Path) -> pd.DataFrame:
    """
    Informe quadrimestral de FIP (abr/ago/dez) — única fonte CVM de VP/cota para
    FIP-IE listados (ex.: AZIN11). Retorna df com N, data, vp, pl, cotas (classe 1).
    """
    frames = []
    for ano in range(HOJE.year - anos, HOJE.year + 1):
        dest = cache_dir / f'inf_quadrimestral_fip_{ano}.csv'
        if not baixar_arquivo(CVM_FIP_URL.format(ano=ano), dest, max_idade_dias=(1 if ano == HOJE.year else None), timeout=120):
            continue
        try:
            d = pd.read_csv(dest, sep=';', encoding='latin-1', dtype=str, low_memory=False)
        except Exception as e:
            log.warning(f"  ✗ {dest.name}: {e}"); continue
        if 'CNPJ_FUNDO_CLASSE' not in d.columns:
            continue
        frames.append(d)
    if not frames:
        return pd.DataFrame()
    d = pd.concat(frames, ignore_index=True)
    d['N'] = d['CNPJ_FUNDO_CLASSE'].map(N)
    d['data'] = pd.to_datetime(d['DT_COMPTC'], errors='coerce')
    d['vp'] = d['VL_QUOTA_CLASSE'].map(fnum)
    d['pl'] = d['VL_PATRIM_LIQ'].map(fnum)
    d['cotas'] = d['QT_COTA_INTEGR_CLASSE'].map(fnum)
    d = d[d['data'].notna() & d['vp'].notna() & (d['vp'] > 0)]
    # classe 1 (cotas negociadas); dedup por data mantendo a maior quantidade de cotas
    d = d[(d['CLASSE_COTA'].fillna('1') == '1')].sort_values(['N', 'data', 'cotas']).drop_duplicates(['N', 'data'], keep='last')
    log.info(f"CVM FIP quadrimestral: {d['N'].nunique()} fundos | última ref {d['data'].max().date() if len(d) else '-'}")
    return d[['N', 'data', 'vp', 'pl', 'cotas']]


def filtrar_outliers_vp(serie: pd.Series, tol: float = 0.25) -> pd.Series:
    """
    Remove pontos de VP que destoam > tol dos dois vizinhos quando os vizinhos
    concordam entre si (±10%). Caso real: AZIN11 abr/2026 com VP 48,91 entre
    97,10 e 97,90 — a CVM registrou o dobro de cotas naquele informe.
    """
    s = serie.dropna().sort_index()
    if len(s) < 3:
        return s
    keep = []
    v = s.values
    for i in range(len(v)):
        if 0 < i < len(v) - 1:
            a, b = v[i - 1], v[i + 1]
            if abs(a / b - 1) <= 0.10 and (abs(v[i] / a - 1) > tol and abs(v[i] / b - 1) > tol):
                log.warning(f"  [VP_OUTLIER] {s.index[i].date()}: VP {v[i]:.2f} descartado (vizinhos {a:.2f} / {b:.2f})")
                continue
        keep.append(i)
    return s.iloc[keep]


# ---------------------------------------------------------------------------
# 6. CVM — CDA (carteira) com look-through
# ---------------------------------------------------------------------------

def localizar_cda(cache_dir: Path, forcar: str | None) -> Path | None:
    cands = [forcar] if forcar else [f"{y}{m:02d}" for (y, m) in (meses_atras(k) for k in range(1, 5))]
    for aaaamm in cands:
        dest = cache_dir / f'cda_fi_{aaaamm}.zip'
        # o mês corrente/anterior pode estar publicado só parcialmente (zip de poucos KB);
        # exige tamanho mínimo para considerar o mês "fechado" — senão volta um mês.
        ok = dest.exists() or baixar_arquivo(CVM_CDA_URL.format(aaaamm=aaaamm), dest, max_idade_dias=1, timeout=600)
        if ok and dest.stat().st_size > 3_000_000:
            log.info(f"CDA: usando {dest.name} ({dest.stat().st_size/1e6:.1f} MB)")
            return dest
        if ok:
            log.info(f"CDA: {dest.name} ainda parcial ({dest.stat().st_size/1e3:.0f} KB) — tentando mês anterior")
    log.warning("CDA: nenhum mês disponível")
    return None


def carregar_cda(zip_path: Path) -> dict:
    blocos = {}
    for k in ['PL', 'BLC_1', 'BLC_2', 'BLC_4', 'BLC_5', 'BLC_6', 'BLC_8', 'CONFID']:
        d = ler_csv_zip(zip_path, f'cda_fi_{k}_')
        if k == 'CONFID':  # evita pegar cda_fie_CONFID
            d = ler_csv_zip(zip_path, 'cda_fi_CONFID_')
        if not d.empty:
            d['N'] = d['CNPJ_FUNDO_CLASSE'].map(N)
            for c in ['VL_MERC_POS_FINAL', 'VL_PATRIM_LIQ', 'QT_POS_FINAL']:
                if c in d.columns:
                    d[c] = pd.to_numeric(d[c], errors='coerce')
        blocos[k] = d
    log.info(f"  CDA carregada: " + ', '.join(f"{k}={len(v)}" for k, v in blocos.items()))
    return blocos


def pl_cda(blocos: dict, n: str) -> float | None:
    pl = blocos.get('PL', pd.DataFrame())
    if pl.empty:
        return None
    s = pl[pl['N'] == n]['VL_PATRIM_LIQ']
    return float(s.iloc[0]) if len(s) and pd.notna(s.iloc[0]) else None


def posicoes_diretas(blocos: dict, n: str) -> list[dict]:
    """Posições diretas de um CNPJ na CDA (todos os blocos relevantes), já classificadas."""
    out = []
    for bloco, d in blocos.items():
        if bloco in ('PL', 'CONFID') or d.empty:
            continue
        s = d[d['N'] == n]
        for _, r in s.iterrows():
            v = r.get('VL_MERC_POS_FINAL')
            if v is None or pd.isna(v) or v == 0:
                continue
            tp = str(r.get('TP_APLIC', ''))
            item = {'bloco': bloco, 'tp_aplic': tp, 'valor': float(v)}
            if bloco == 'BLC_2':
                item.update(classe='Cotas de fundos', cnpj_cota=N(r.get('CNPJ_FUNDO_CLASSE_COTA')),
                            nome=str(r.get('NM_FUNDO_CLASSE_SUBCLASSE_COTA', ''))[:80])
            elif bloco == 'BLC_4':
                item.update(classe=('Debêntures' if 'Deb' in tp else tp), codigo=str(r.get('CD_ATIVO', '')).strip(),
                            isin=str(r.get('CD_ISIN', '')).strip(), vencimento=str(r.get('DT_FIM_VIGENCIA', '') or '')[:10])
            elif bloco == 'BLC_6':
                item.update(classe=tp, emissor=str(r.get('EMISSOR', ''))[:80], cnpj_emissor=N(r.get('CPF_CNPJ_EMISSOR')),
                            vencimento=str(r.get('DT_VENC', '') or '')[:10], indexador=str(r.get('DS_INDEXADOR_POSFX', '') or ''),
                            cupom=fnum(r.get('PR_CUPOM_POSFX')))
            elif bloco == 'BLC_5':
                item.update(classe='Títulos de IF', emissor=str(r.get('EMISSOR', ''))[:80], cnpj_emissor=N(r.get('CNPJ_EMISSOR')),
                            vencimento=str(r.get('DT_VENC', '') or '')[:10], indexador=str(r.get('DS_INDEXADOR_POSFX', '') or ''))
            elif bloco == 'BLC_1':
                item.update(classe='Títulos públicos', nome=str(r.get('TP_TITPUB', '')), vencimento=str(r.get('DT_VENC', '') or '')[:10])
            elif bloco == 'BLC_8':
                item.update(classe=tp)
            out.append(item)
    return out


def confidencial_cda(blocos: dict, n: str) -> float:
    c = blocos.get('CONFID', pd.DataFrame())
    if c.empty:
        return 0.0
    s = c[c['N'] == n]['VL_MERC_POS_FINAL']
    return float(s.sum()) if len(s) else 0.0


def look_through(blocos: dict, n_raiz: str, profundidade: int = 3) -> dict:
    """
    Explode cotas de fundos recursivamente. Retorna lista de posições finais com
    `valor_lt` (valor look-through atribuído ao fundo raiz) e metadados.
    """
    pl_raiz = pl_cda(blocos, n_raiz)
    finais, caminhos, confid_total = [], [], 0.0
    visitados = set()

    def explode(n: str, fator: float, nivel: int, trilha: list[str]):
        if n in visitados and nivel > 0:
            return
        visitados.add(n)
        pl_n = pl_cda(blocos, n)
        confid = confidencial_cda(blocos, n)
        nonlocal confid_total
        if confid and pl_n:
            confid_total += confid * fator
            caminhos.append({'cnpj': n, 'nivel': nivel, 'confidencial': confid * fator})
        for p in posicoes_diretas(blocos, n):
            v_lt = p['valor'] * fator
            if p['bloco'] == 'BLC_2' and p.get('cnpj_cota') and nivel < profundidade:
                pl_m = pl_cda(blocos, p['cnpj_cota'])
                if pl_m and pl_m > 0 and (posicoes_diretas(blocos, p['cnpj_cota']) or confidencial_cda(blocos, p['cnpj_cota'])):
                    explode(p['cnpj_cota'], fator * p['valor'] / pl_m, nivel + 1, trilha + [p.get('nome', '')])
                    continue
                # fundo-cota sem CDA aberta → fica como "cotas de fundos"
            q = dict(p); q['valor_lt'] = v_lt; q['nivel'] = nivel; q['via'] = ' > '.join(trilha) if trilha else ''
            finais.append(q)

    explode(n_raiz, 1.0, 0, [])
    return {'pl': pl_raiz, 'posicoes': finais, 'confidencial': confid_total, 'confid_caminhos': caminhos}


# ---------------------------------------------------------------------------
# 7. ANBIMA — SND (características das debêntures)
# ---------------------------------------------------------------------------

def carregar_snd(cache_dir: Path) -> pd.DataFrame:
    dest = cache_dir / 'snd_caracteristicas.txt'
    baixar_arquivo(SND_URL, dest, max_idade_dias=1, timeout=180)
    if not dest.exists():
        return pd.DataFrame()
    raw = dest.read_bytes().decode('latin-1')
    lines = raw.splitlines()
    hi = next((i for i, l in enumerate(lines) if 'Codigo do Ativo' in l and 'ISIN' in l), None)
    if hi is None:
        return pd.DataFrame()
    df = pd.read_csv(io.StringIO('\n'.join(lines[hi:])), sep='\t', dtype=str, on_bad_lines='skip')
    df.columns = [c.strip() for c in df.columns]
    df = df.rename(columns={'Codigo do Ativo': 'codigo', 'Empresa': 'emissor', 'CNPJ': 'cnpj_emissor', 'indice': 'indexador',
                            'Juros Criterio Novo - Taxa': 'taxa', 'Deb. Incent. (Lei 12.431)': 'incentivada',
                            'Data de Vencimento': 'vencimento', 'Situacao': 'situacao', 'Motivo de Saida': 'motivo_saida',
                            'Garantia/Especie': 'garantia', 'Percentual Multiplicador/Rentabilidade': 'pct_indexador'})
    for c in ['codigo', 'emissor', 'cnpj_emissor', 'indexador', 'taxa', 'incentivada', 'vencimento', 'situacao', 'motivo_saida', 'garantia', 'pct_indexador']:
        if c in df.columns:
            df[c] = df[c].astype(str).str.strip()
    df = df[df['codigo'].notna() & (df['codigo'] != '')].drop_duplicates('codigo')
    log.info(f"SND: {len(df)} debêntures carregadas")
    return df.set_index('codigo')


def classificar_indexador(ind: str, pct: str | None = None) -> str:
    s = norm_txt(ind)
    if 'IPCA' in s or 'IGP' in s or 'INPC' in s:
        return 'IPCA+'
    if s in ('DI', 'CDI') or 'DI' == s[:2] or 'CDI' in s:
        return 'CDI'
    if 'PRE' in s:
        return 'Pré'
    if s == '' or s == 'NAN' or 'SEM' in s:
        return 'Outros'
    return s.title()


# ---------------------------------------------------------------------------
# 8. Eventos de stress
# ---------------------------------------------------------------------------

def carregar_eventos(path: Path) -> list[dict]:
    if not path.exists():
        return []
    ev = json.loads(path.read_text(encoding='utf-8')).get('eventos', [])
    log.info(f"Eventos de stress: {len(ev)} carregados")
    return ev


def casar_evento_emissor(ev: dict, emissor: str, cnpj_emissor: str, codigo: str) -> bool:
    if ev.get('cnpj_emissor') and cnpj_emissor and N(ev['cnpj_emissor']) == cnpj_emissor:
        return True
    pad = ev.get('emissor_regex')
    if pad and emissor and re.search(pad, norm_txt(emissor)):
        return True
    cods = ev.get('codigos') or []
    if codigo and codigo in cods:
        return True
    prefixos = ev.get('prefixo_codigo') or []
    if codigo and any(codigo.upper().startswith(p.upper()) for p in prefixos):
        return True
    return False


# ---------------------------------------------------------------------------
# Métricas
# ---------------------------------------------------------------------------

def _asof(serie: pd.Series, data: pd.Timestamp, inclusive=True):
    s = serie[serie.index <= data] if inclusive else serie[serie.index < data]
    return float(s.iloc[-1]) if len(s) else None


def metricas_preco(close: pd.Series, vol_fin: pd.Series, divs: pd.Series) -> dict:
    close = close.dropna().sort_index()
    t = close.index[-1]; p = float(close.iloc[-1])
    def var_desde(base):
        v0 = _asof(close, base, inclusive=True)
        return r4(p / v0 - 1, 6) if v0 else None
    p_ano0 = _asof(close, pd.Timestamp(t.year, 1, 1), inclusive=False)
    ret = close.pct_change().dropna()
    r12 = ret[ret.index > t - pd.DateOffset(months=12)]
    c12 = close[close.index > t - pd.DateOffset(months=12)]
    dd12 = float((c12 / c12.cummax() - 1).min()) if len(c12) > 5 else None
    d12 = divs[(divs.index > t - pd.DateOffset(months=12)) & (divs.index <= t)]
    d1m = divs[(divs.index > t - pd.DateOffset(months=1)) & (divs.index <= t)]
    ult = divs.index.max() if len(divs) else None
    p_12m = _asof(close, t - pd.DateOffset(months=12))
    out = {
        'preco': r4(p, 4), 'data_preco': t.strftime('%Y-%m-%d'),
        'var_1m': var_desde(t - pd.DateOffset(months=1)),
        'var_ano': r4(p / p_ano0 - 1, 6) if p_ano0 else None,
        'var_12m': var_desde(t - pd.DateOffset(months=12)),
        'ret_total_12m': r4((p + float(d12.sum())) / p_12m - 1, 6) if p_12m else None,
        'vol_12m': r4(float(r12.std() * math.sqrt(DIAS_UTEIS_ANO)), 6) if len(r12) > 20 else None,
        'drawdown_12m': r4(dd12, 6),
        'volume_medio_3m_mil': r4(float(vol_fin[vol_fin.index > t - pd.DateOffset(months=3)].mean()) / 1e3, 1) if len(vol_fin) else None,
        'provento_ultimo': r4(float(divs.loc[ult]), 6) if ult is not None else None,
        'provento_ultimo_data': ult.strftime('%Y-%m-%d') if ult is not None else None,
        'proventos_12m': r4(float(d12.sum()), 6),
        'proventos_1m': r4(float(d1m.sum()), 6),
        'n_proventos_12m': int(len(d12)),
        'dy_1m': r4(float(d1m.sum()) / p, 6) if p else None,
        'dy_12m': r4(float(d12.sum()) / p, 6) if p else None,
        'yield_anualizado': r4(float(divs.loc[ult]) * 12 / p, 6) if (ult is not None and p) else None,
    }
    return out


# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description='Pipeline Fundos Listados (RF)')
    ap.add_argument('--universo', default='./data/listados_universo.json')
    ap.add_argument('--eventos', default='./data/listados_eventos_stress.json')
    ap.add_argument('--output', default='./data')
    ap.add_argument('--anos', type=int, default=3, help='anos de histórico CVM diário (FI-Infra)')
    ap.add_argument('--anos-preco', type=int, default=5)
    ap.add_argument('--cda', default=None, help='AAAAMM da CDA (padrão: mais recente disponível)')
    ap.add_argument('--sem-cda', action='store_true')
    ap.add_argument('--sem-yahoo', action='store_true')
    args = ap.parse_args()

    out_dir = Path(args.output); cache = out_dir / 'cache_cvm_listados'; cache.mkdir(parents=True, exist_ok=True)
    log.info("=" * 70); log.info("ALPHA DESK — Pipeline Fundos Listados"); log.info("=" * 70)

    fundos = carregar_universo(Path(args.universo))
    eventos = carregar_eventos(Path(args.eventos))
    por_ticker = {f['ticker']: f for f in fundos}

    # ---- Yahoo
    yahoo = {} if args.sem_yahoo else baixar_yahoo([f['ticker'] for f in fundos], anos=args.anos_preco)

    # ---- CVM diário (FI-Infra)
    cn_fi = {f['cnpj'] for f in fundos if f['tipo'] == 'FI-Infra'}
    df_diario = baixar_cvm_diario(cn_fi, args.anos, cache)
    diario_por_cnpj = {}
    if not df_diario.empty:
        for n, g in df_diario.groupby('CNPJ_NORM'):
            g = g.sort_values('DT_COMPTC').drop_duplicates('DT_COMPTC', keep='last').set_index('DT_COMPTC')
            diario_por_cnpj[n] = g

    # ---- CVM mensal FII / FIAGRO
    fii = baixar_cvm_fii(args.anos, cache)
    fiagro = baixar_cvm_fiagro(cache)
    fip = baixar_cvm_fip(args.anos, cache) if any(f['tipo'] == 'FIP-IE' for f in fundos) else pd.DataFrame()

    # ---- CDA + SND
    blocos, snd = {}, pd.DataFrame()
    cda_ref = None
    if not args.sem_cda:
        zp = localizar_cda(cache, args.cda)
        if zp:
            blocos = carregar_cda(zp)
            cda_ref = zp.stem.replace('cda_fi_', '')
            snd = carregar_snd(cache)

    # ---- Monta registros
    registros, series, carteiras = [], {}, {}
    datas_ref = {'yahoo': None, 'cvm_diario': None, 'cvm_fii': None, 'cvm_fiagro': None, 'cvm_fip': None, 'cda': cda_ref}

    for f in fundos:
        t, n, tipo = f['ticker'], N(f['cnpj']), f['tipo']
        reg = {k: f.get(k) for k in ['ticker', 'cnpj', 'tipo', 'categoria', 'indexador', 'nome', 'gestor', 'administrador',
                                     'taxa_adm', 'status', 'no_painel', 'tir_relatorio', 'obs']}
        reg['rent_fonte_vp'] = None
        ser = {'datas': [], 'preco': [], 'vp': [], 'pvp': [], 'dividendos': []}

        # --- preço
        y = yahoo.get(t)
        if y:
            reg.update(metricas_preco(y['close'], y['vol_fin'], y['dividends']))
            datas_ref['yahoo'] = max(datas_ref['yahoo'] or '', reg['data_preco'])
            close = y['close'].dropna()
            ser['dividendos'] = [{'data': d.strftime('%Y-%m-%d'), 'valor': r4(float(v), 6)} for d, v in y['dividends'].items()]
        else:
            close = pd.Series(dtype=float)

        # --- VP (cota patrimonial), PL, cotas emitidas, cotistas
        vp_serie = pd.Series(dtype=float); pl_serie = pd.Series(dtype=float); cotas_emitidas = None; cotistas = None
        fonte_vp = None; aloc = {}; dy_cvm = None; rent_pat_mes = None
        if tipo == 'FI-Infra' and n in diario_por_cnpj:
            g = diario_por_cnpj[n]
            vp_serie = g['VL_QUOTA'].astype(float); pl_serie = g['VL_PATRIM_LIQ'].astype(float)
            cot = g['NR_COTST'].dropna(); cotistas = int(cot.iloc[-1]) if len(cot) else None
            if len(vp_serie) and vp_serie.iloc[-1] > 0:
                cotas_emitidas = float(pl_serie.iloc[-1] / vp_serie.iloc[-1])
            fonte_vp = 'CVM informe diário'
            datas_ref['cvm_diario'] = max(datas_ref['cvm_diario'] or '', vp_serie.index.max().strftime('%Y-%m-%d'))
        if tipo in ('FII', 'Fiagro'):
            comp = fii['complemento']
            cfii = comp[comp['N'] == n].copy() if not comp.empty else pd.DataFrame()
            rows = []
            if not cfii.empty:
                for _, r in cfii.iterrows():
                    rows.append((r['Data_Referencia'], fnum(r['Valor_Patrimonial_Cotas']), fnum(r['Patrimonio_Liquido']),
                                 fnum(r['Cotas_Emitidas']), fnum(r['Total_Numero_Cotistas']), fnum(r.get('Percentual_Dividend_Yield_Mes')),
                                 fnum(r.get('Percentual_Rentabilidade_Patrimonial_Mes')), 'CVM informe mensal FII'))
            fm = fiagro['main']
            cf = fm[fm['N'] == n].copy() if not fm.empty else pd.DataFrame()
            if not cf.empty:
                for _, r in cf.iterrows():
                    rows.append((r['Data_Referencia'], fnum(r.get('Valor_Patrimonial_Cotas')), fnum(r.get('Patrimonio_Liquido')),
                                 fnum(r.get('Cotas_Emitidas')), fnum(r.get('Numero_Cotistas')), fnum(r.get('Dividend_Yield_Mes')),
                                 fnum(r.get('Rentabilidade_Patrimonial_Mes')), 'CVM informe mensal FIAGRO'))
            if rows:
                m = pd.DataFrame(rows, columns=['ref', 'vp', 'pl', 'cotas', 'cotistas', 'dy_mes', 'rent_pat', 'fonte'])
                m['ref'] = pd.to_datetime(m['ref'], errors='coerce'); m = m.dropna(subset=['ref'])
                m = m[m['vp'].notna() & (m['vp'] > 0)].sort_values(['ref', 'fonte']).drop_duplicates('ref', keep='last')
                # referência mensal = posição de fim do mês → data = último dia do mês
                m['data'] = m['ref'] + pd.offsets.MonthEnd(0)
                vp_serie = pd.Series(m['vp'].values, index=m['data']); pl_serie = pd.Series(m['pl'].values, index=m['data'])
                ult = m.iloc[-1]
                cotas_emitidas = ult['cotas'] if pd.notna(ult['cotas']) else None
                cotistas = int(ult['cotistas']) if pd.notna(ult['cotistas']) else None
                dy_cvm = r4(ult['dy_mes'] / 100, 6) if pd.notna(ult['dy_mes']) else None
                rent_pat_mes = r4(ult['rent_pat'] / 100, 6) if pd.notna(ult['rent_pat']) else None
                fonte_vp = ult['fonte']
                key = 'cvm_fiagro' if 'FIAGRO' in fonte_vp else 'cvm_fii'
                datas_ref[key] = max(datas_ref[key] or '', ult['ref'].strftime('%Y-%m-%d'))
            # alocação (ativo/passivo) — FII dataset
            ap_ = fii['ativo_passivo']
            a = ap_[ap_['N'] == n].sort_values('Data_Referencia') if not ap_.empty else pd.DataFrame()
            if not a.empty:
                r = a.iloc[-1]
                tot = fnum(r.get('Total_Investido')) or 0
                campos = {'CRI': 'CRI', 'CRI_CRA': 'CRI/CRA', 'LCI_LCA': 'LCI/LCA', 'LCI': 'LCI', 'LIG': 'LIG', 'Debentures': 'Debêntures',
                          'FII': 'Cotas de FII', 'FDIC': 'FIDC', 'Outras_Cotas_FI': 'Outras cotas de FI', 'Fundos_Renda_Fixa': 'Fundos RF (caixa)',
                          'Titulos_Publicos': 'Títulos públicos', 'Disponibilidades': 'Disponibilidades', 'Acoes': 'Ações',
                          'Direitos_Bens_Imoveis': 'Imóveis', 'Outros_Valores_Mobliarios': 'Outros VM', 'Titulos_Privados': 'Títulos privados'}
                base = (fnum(r.get('Total_Investido')) or 0) + (fnum(r.get('Total_Necessidades_Liquidez')) or 0)
                for c, label in campos.items():
                    v = fnum(r.get(c))
                    if v and base:
                        aloc[label] = r4(v / base, 4)
                aloc['_ref'] = r['Data_Referencia']; aloc['_fonte'] = 'CVM ativo/passivo FII'
            elif not cf.empty:
                r = cf.sort_values('Data_Referencia').iloc[-1]
                base = fnum(r.get('Valor_Ativo')) or 0
                campos = {'CRA': 'CRA', 'CRI': 'CRI', 'Titulos_Divida_Corporativa': 'Dívida corporativa', 'Cotas_Fundos_Investimento': 'Cotas de FI',
                          'Fundos_Renda_Fixa': 'Fundos RF (caixa)', 'Titulos_Renda_Fixa': 'Títulos RF', 'Valores_Mobiliarios': 'Valores mobiliários',
                          'Valor_Titulos_Credito': 'Títulos de crédito'}
                for c, label in campos.items():
                    v = fnum(r.get(c))
                    if v and base:
                        aloc[label] = r4(v / base, 4)
                aloc['_ref'] = r['Data_Referencia']; aloc['_fonte'] = 'CVM informe FIAGRO'
        if tipo == 'FIP-IE' and not fip.empty:
            q = fip[fip['N'] == n].sort_values('data')
            if len(q):
                vp_serie = filtrar_outliers_vp(pd.Series(q['vp'].values, index=q['data']))
                pl_serie = pd.Series(q['pl'].values, index=q['data']).reindex(vp_serie.index)
                c_ult = q['cotas'].dropna()
                cotas_emitidas = float(c_ult.iloc[-1]) if len(c_ult) else None
                fonte_vp = 'CVM informe quadrimestral FIP'
                datas_ref['cvm_fip'] = max(datas_ref['cvm_fip'] or '', vp_serie.index.max().strftime('%Y-%m-%d'))
        if f.get('vp_manual'):
            # ponto manual (relatório gerencial) complementa a série quando é mais recente que a CVM
            vm = f['vp_manual']; dm = pd.Timestamp(vm['data'])
            if vp_serie.empty or dm > vp_serie.index.max():
                vp_serie = pd.concat([vp_serie, pd.Series([float(vm['valor'])], index=[dm])]).sort_index()
                fonte_vp = (fonte_vp + ' + ' if fonte_vp else '') + f"manual ({vm.get('fonte', 'relatório')})"
                if f.get('cotas_emitidas_manual'):
                    cotas_emitidas = f['cotas_emitidas_manual']
                if cotas_emitidas:
                    pl_serie = pd.concat([pl_serie, pd.Series([float(cotas_emitidas) * float(vm['valor'])], index=[dm])]).sort_index()

        # --- consolidação VP / P/VP
        if len(vp_serie):
            vp_serie = vp_serie.sort_index()
            reg['vp_cota'] = r4(float(vp_serie.iloc[-1]), 4); reg['vp_data'] = vp_serie.index[-1].strftime('%Y-%m-%d')
            reg['pl'] = r4(float(pl_serie.iloc[-1]) / 1e6, 2) if len(pl_serie) else None
            reg['cotas_emitidas'] = r4(cotas_emitidas, 0)
            reg['cotistas'] = cotistas
            reg['rent_fonte_vp'] = fonte_vp
            if reg.get('preco'):
                reg['p_vp'] = r4(reg['preco'] / reg['vp_cota'], 4)
                reg['vm'] = r4(reg['preco'] * cotas_emitidas / 1e6, 2) if cotas_emitidas else None
                reg['vm_pl'] = r4(reg['vm'] / reg['pl'], 4) if (reg.get('vm') and reg.get('pl')) else None
            # P/VP 12M stats
            if len(close):
                vp_d = vp_serie.reindex(close.index.union(vp_serie.index)).sort_index().ffill().reindex(close.index)
                pvp = (close / vp_d).dropna()
                pvp12 = pvp[pvp.index > close.index[-1] - pd.DateOffset(months=12)]
                reg['p_vp_min_12m'] = r4(float(pvp12.min()), 4) if len(pvp12) else None
                reg['p_vp_max_12m'] = r4(float(pvp12.max()), 4) if len(pvp12) else None
                reg['p_vp_medio_12m'] = r4(float(pvp12.mean()), 4) if len(pvp12) else None
                # séries para o gráfico (amostra diária; VP ffill)
                idx = close.index
                ser['datas'] = [d.strftime('%Y-%m-%d') for d in idx]
                ser['preco'] = [r4(float(v), 4) for v in close.values]
                ser['vp'] = [r4(float(v), 4) if pd.notna(v) else None for v in vp_d.values]
                ser['pvp'] = [r4(float(v), 4) if pd.notna(v) else None for v in (close / vp_d).values]
                ser['vp_fonte'] = fonte_vp
                ser['vp_frequencia'] = 'diária' if tipo == 'FI-Infra' else ('quadrimestral' if tipo == 'FIP-IE' else 'mensal')
        elif len(close):
            ser['datas'] = [d.strftime('%Y-%m-%d') for d in close.index]
            ser['preco'] = [r4(float(v), 4) for v in close.values]
        reg['dy_mes_cvm'] = dy_cvm; reg['rent_patrimonial_mes_cvm'] = rent_pat_mes
        reg['alocacao'] = aloc

        # --- carteira (CDA look-through) — FI-Infra
        cart = {'fonte': None, 'cobertura': None, 'confidencial_pct': None, 'n_ativos': None, 'indexadores': {}, 'cupom_medio': {},
                'ativos': [], 'top_emissores': [], 'alertas': [], 'classes': {}}
        if blocos and tipo == 'FI-Infra':
            lt = look_through(blocos, n)
            pl_r = lt['pl']
            if pl_r:
                cart['fonte'] = f"CVM CDA {cda_ref} (look-through FIC → master)"
                cart['pl_cda'] = r4(pl_r / 1e6, 2)
                # confidencial pode somar > 100% do PL quando o FIC tem vários masters
                # totalmente confidenciais (fator × PL_master em cada nível) — cap em 100%
                cart['confidencial_pct'] = r4(min(1.0, lt['confidencial'] / pl_r), 4)
                debs, classes = [], {}
                for p in lt['posicoes']:
                    classes[p['classe']] = classes.get(p['classe'], 0) + p['valor_lt']
                    if p['classe'] == 'Debêntures':
                        cod = p.get('codigo', '')
                        s = snd.loc[cod] if (not snd.empty and cod in snd.index) else None
                        d = {'codigo': cod, 'isin': p.get('isin'), 'vencimento': p.get('vencimento') or (s['vencimento'] if s is not None else None),
                             'valor_mil': r4(p['valor_lt'] / 1e3, 1), 'pct_pl': r4(p['valor_lt'] / pl_r, 5), 'via': p.get('via'),
                             'emissor': (s['emissor'] if s is not None else None), 'cnpj_emissor': (s['cnpj_emissor'] if s is not None else None),
                             'indexador': classificar_indexador(s['indexador']) if s is not None else 'n/d',
                             'taxa': (fnum(s['taxa']) if s is not None else None),
                             'incentivada': ((s['incentivada'] == 'S') if s is not None else None),
                             'garantia': (s['garantia'] if s is not None else None),
                             'situacao_snd': (s['situacao'] if s is not None else None), 'motivo_saida': (s['motivo_saida'] if s is not None else None)}
                        debs.append(d)
                # agrega debêntures iguais (mesmo código via caminhos diferentes)
                agg = {}
                for d in debs:
                    k = d['codigo']
                    if k in agg:
                        agg[k]['valor_mil'] = r4((agg[k]['valor_mil'] or 0) + (d['valor_mil'] or 0), 1)
                        agg[k]['pct_pl'] = r4((agg[k]['pct_pl'] or 0) + (d['pct_pl'] or 0), 5)
                    else:
                        agg[k] = d
                debs = sorted(agg.values(), key=lambda x: -(x['pct_pl'] or 0))
                tot_deb = sum((d['pct_pl'] or 0) for d in debs)
                cart['n_ativos'] = len(debs)
                cart['classes'] = {k: r4(v / pl_r, 4) for k, v in sorted(classes.items(), key=lambda kv: -kv[1])}
                cart['cobertura'] = r4(sum(classes.values()) / pl_r, 4)
                idx_split, cupom_w, cupom_n = {}, {}, {}
                for d in debs:
                    ix = d['indexador'] or 'n/d'
                    idx_split[ix] = idx_split.get(ix, 0) + (d['pct_pl'] or 0)
                    if d['taxa'] is not None:
                        cupom_w[ix] = cupom_w.get(ix, 0) + d['taxa'] * (d['pct_pl'] or 0)
                        cupom_n[ix] = cupom_n.get(ix, 0) + (d['pct_pl'] or 0)
                cart['indexadores'] = {k: r4(v / tot_deb, 4) for k, v in idx_split.items()} if tot_deb else {}
                cart['cupom_medio'] = {k: r4(cupom_w[k] / cupom_n[k], 3) for k in cupom_w if cupom_n.get(k)}
                em = {}
                for d in debs:
                    e = d['emissor'] or f"(código {d['codigo']})"
                    em[e] = em.get(e, 0) + (d['pct_pl'] or 0)
                cart['top_emissores'] = [{'emissor': k, 'pct_pl': r4(v, 5)} for k, v in sorted(em.items(), key=lambda kv: -kv[1])[:15]]
                cart['n_emissores'] = len(em)
                cart['pct_incentivadas'] = r4(sum((d['pct_pl'] or 0) for d in debs if d['incentivada']) / tot_deb, 4) if tot_deb else None
                cart['ativos'] = debs
                # vencimento médio ponderado (anos)
                vs = [(pd.to_datetime(d['vencimento'], errors='coerce', dayfirst=('/' in str(d['vencimento']))), d['pct_pl'] or 0) for d in debs if d['vencimento']]
                vs = [(v, w) for v, w in vs if pd.notna(v)]
                if vs and sum(w for _, w in vs):
                    cart['prazo_medio_anos'] = r4(sum(((v - pd.Timestamp(HOJE)).days / 365.25) * w for v, w in vs) / sum(w for _, w in vs), 2)
                # alertas: eventos de emissor
                for ev in eventos:
                    if ev.get('escopo') != 'emissor':
                        continue
                    exp = 0.0; cods = []
                    for d in debs:
                        if casar_evento_emissor(ev, d.get('emissor') or '', d.get('cnpj_emissor') or '', d['codigo']):
                            exp += d['pct_pl'] or 0; cods.append(d['codigo'])
                    if cods:
                        cart['alertas'].append({'id': ev['id'], 'titulo': ev['titulo'], 'severidade': ev.get('severidade', 'media'),
                                                'tipo': ev.get('tipo'), 'data': ev.get('data'), 'exposicao_pct_pl': r4(exp, 5), 'codigos': cods,
                                                'descricao': ev.get('descricao'), 'fonte': ev.get('fonte')})
                # alertas SND: debênture com situação ≠ Registrado (resgatada, vencida antecipada, etc.)
                for d in debs:
                    st = (d.get('situacao_snd') or '')
                    if st and st != 'Registrado':
                        cart['alertas'].append({'id': f"snd-{d['codigo']}", 'titulo': f"{d['emissor'] or d['codigo']}: situação SND '{st}'",
                                                'severidade': 'baixa', 'tipo': 'situacao_snd', 'data': None,
                                                'exposicao_pct_pl': d['pct_pl'], 'codigos': [d['codigo']],
                                                'descricao': d.get('motivo_saida') or '', 'fonte': 'ANBIMA SND'})
        # eventos de fundo (qualquer tipo)
        for ev in eventos:
            if ev.get('escopo') == 'fundo' and t in (ev.get('tickers') or []):
                cart['alertas'].append({'id': ev['id'], 'titulo': ev['titulo'], 'severidade': ev.get('severidade', 'media'), 'tipo': ev.get('tipo'),
                                        'data': ev.get('data'), 'exposicao_pct_pl': ev.get('exposicao_pct_pl', {}).get(t) if isinstance(ev.get('exposicao_pct_pl'), dict) else ev.get('exposicao_pct_pl'),
                                        'codigos': [], 'descricao': ev.get('descricao'), 'fonte': ev.get('fonte')})
        sev_rank = {'alta': 3, 'media': 2, 'baixa': 1}
        cart['alertas'] = sorted(cart['alertas'], key=lambda a: (-sev_rank.get(a['severidade'], 0), -(a.get('exposicao_pct_pl') or 0)))
        cart['stress_nivel'] = (max((sev_rank.get(a['severidade'], 0) for a in cart['alertas']), default=0))
        cart['stress_exposicao_pct_pl'] = r4(sum((a.get('exposicao_pct_pl') or 0) for a in cart['alertas'] if a['tipo'] != 'situacao_snd'), 5)
        reg['n_ativos'] = cart['n_ativos'] if cart['n_ativos'] is not None else f.get('qtde_ativos_relatorio')
        reg['indexadores_carteira'] = cart['indexadores']
        reg['cupom_medio'] = cart['cupom_medio']
        reg['carteira_fonte'] = cart['fonte'] or (f"CVM ativo/passivo ({aloc.get('_ref')})" if aloc else None)
        reg['carteira_confidencial_pct'] = cart['confidencial_pct']
        reg['stress_nivel'] = cart['stress_nivel']; reg['stress_n_alertas'] = len(cart['alertas']); reg['stress_exposicao_pct_pl'] = cart['stress_exposicao_pct_pl']
        reg['stress_alertas'] = [{'titulo': a['titulo'], 'severidade': a['severidade'], 'exposicao_pct_pl': a.get('exposicao_pct_pl')} for a in cart['alertas'][:5]]

        registros.append(reg); series[t] = ser; carteiras[t] = cart
        log.info(f"  {t:7s} {tipo:8s} px={reg.get('preco')} vp={reg.get('vp_cota')} p/vp={reg.get('p_vp')} pl={reg.get('pl')}M "
                 f"dy12={reg.get('dy_12m')} ativos={reg.get('n_ativos')} stress={reg['stress_nivel']} ({reg['stress_n_alertas']})")

    # ---- Sanitiza e salva
    def clean(o):
        if isinstance(o, dict):
            return {k: clean(v) for k, v in o.items()}
        if isinstance(o, list):
            return [clean(v) for v in o]
        if isinstance(o, float) and not math.isfinite(o):
            return None
        if isinstance(o, (np.floating, np.integer)):
            v = float(o); return v if math.isfinite(v) else None
        if isinstance(o, (pd.Timestamp, datetime, date)):
            return o.strftime('%Y-%m-%d')
        return o

    (out_dir / 'listados.json').write_text(json.dumps(clean(registros), ensure_ascii=False, indent=1), encoding='utf-8')
    (out_dir / 'listados_series.json').write_text(json.dumps(clean(series), ensure_ascii=False), encoding='utf-8')
    (out_dir / 'listados_carteiras.json').write_text(json.dumps(clean(carteiras), ensure_ascii=False, indent=1), encoding='utf-8')
    meta = {
        'ultima_atualizacao': datetime.now().isoformat(),
        'total_fundos': len(registros),
        'no_painel': sum(1 for r in registros if r.get('no_painel')),
        'por_tipo': pd.Series([r['tipo'] for r in registros]).value_counts().to_dict(),
        'por_status': pd.Series([r['status'] for r in registros]).value_counts().to_dict(),
        'datas_referencia': datas_ref,
        'cobertura_vp': {k: int(v) for k, v in pd.Series([r.get('rent_fonte_vp') or 'sem VP' for r in registros]).value_counts().items()},
        'cobertura_carteira': {'cda_look_through': sum(1 for c in carteiras.values() if c.get('fonte')),
                               'ativo_passivo_cvm': sum(1 for r in registros if r.get('alocacao') and not carteiras[r['ticker']].get('fonte')),
                               'sem_carteira': sum(1 for r in registros if not r.get('alocacao') and not carteiras[r['ticker']].get('fonte'))},
        'eventos_stress': len(eventos),
        'fundos_com_alerta': sum(1 for r in registros if r['stress_n_alertas']),
    }
    (out_dir / 'meta_listados.json').write_text(json.dumps(clean(meta), ensure_ascii=False, indent=2), encoding='utf-8')
    log.info("\n" + "=" * 70)
    log.info(f"✓ listados.json ({len(registros)}) | listados_series.json | listados_carteiras.json | meta_listados.json")
    log.info(f"  referências: {datas_ref}")
    log.info(f"  cobertura VP: {meta['cobertura_vp']} | carteira: {meta['cobertura_carteira']} | com alerta: {meta['fundos_com_alerta']}")


if __name__ == '__main__':
    main()
