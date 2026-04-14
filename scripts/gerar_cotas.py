#!/usr/bin/env python3
"""
gerar_cotas.py — Gera cotas.json a partir do cache CVM existente
Não precisa re-baixar nada — usa o cache que já está em data/cache_cvm/

Execute na pasta AlphaDesk:
    python gerar_cotas.py
"""
import json
import glob
import re
import sys
from pathlib import Path
from datetime import datetime

try:
    import pandas as pd
except ImportError:
    sys.exit("ERRO: pip install pandas pyarrow")

DATA_DIR  = Path('./data')
CACHE_DIR = DATA_DIR / 'cache_cvm'

def normalizar(cnpj):
    return re.sub(r'[./-]', '', str(cnpj).strip())

print(f"[{datetime.now().strftime('%H:%M:%S')}] Gerando cotas.json...")

# 1. Load fundos.json
with open(DATA_DIR / 'fundos.json', encoding='utf-8') as f:
    fundos = json.load(f)
cnpj_map = {normalizar(f['cnpj']): f['cnpj'] for f in fundos}
print(f"  fundos.json: {len(fundos)} fundos")

# 2. Load cache
parquets = sorted(glob.glob(str(CACHE_DIR / '*.parquet')))
print(f"  cache_cvm: {len(parquets)} arquivos")
if not parquets:
    sys.exit("ERRO: nenhum parquet em data/cache_cvm/. Rode o pipeline completo primeiro.")

frames = [pd.read_parquet(pf) for pf in parquets]
df = pd.concat(frames, ignore_index=True)
print(f"  Total: {len(df):,} linhas, {df['CNPJ_NORM'].nunique()} CNPJs únicos")

# 3. Normalize dates
df['DT_COMPTC'] = pd.to_datetime(df['DT_COMPTC'], errors='coerce')

# 4. Find VL_QUOTA column
quota_col = next(
    (c for c in df.columns if 'QUOTA' in c.upper() or 'VL_COTA' in c.upper()), None
)
if not quota_col:
    sys.exit(f"ERRO: coluna VL_QUOTA não encontrada. Colunas: {list(df.columns)}")
if quota_col != 'VL_QUOTA':
    df = df.rename(columns={quota_col: 'VL_QUOTA'})

df['VL_QUOTA'] = pd.to_numeric(df['VL_QUOTA'], errors='coerce')

# 5. Build cotas dict
df = df.sort_values(['CNPJ_NORM', 'DT_COMPTC'])
cotas_dict = {}
matched = 0

for cnpj_norm, grp in df.groupby('CNPJ_NORM'):
    cnpj_orig = cnpj_map.get(cnpj_norm)
    if not cnpj_orig:
        continue
    grp = grp.sort_values('DT_COMPTC')
    # Dedup por continuidade: mantém cota mais próxima do dia anterior
    # (evita pegar subclasses CVM 2025 que começam perto de 1.0)
    indices_manter = []
    prev_val = None
    for dt, dia_rows in grp.groupby('DT_COMPTC', sort=True):
        if len(dia_rows) == 1:
            idx = dia_rows.index[0]
            prev_val = dia_rows.iloc[0]['VL_QUOTA']
        else:
            if prev_val is not None and pd.notna(prev_val):
                diffs = (dia_rows['VL_QUOTA'] - prev_val).abs()
                idx = diffs.idxmin()
            else:
                idx = dia_rows['VL_QUOTA'].idxmax()
            prev_val = grp.loc[idx, 'VL_QUOTA']
        indices_manter.append(idx)
    grp = grp.loc[indices_manter].set_index('DT_COMPTC')
    cotas = grp['VL_QUOTA'].dropna()
    if len(cotas) < 2:
        continue
    base = cotas.iloc[0]
    norm = (cotas / base * 100).round(4)
    cotas_dict[cnpj_orig] = {
        'datas':   [d.strftime('%Y-%m-%d') for d in norm.index],
        'valores': norm.tolist(),
    }
    matched += 1

print(f"  Séries geradas: {matched} fundos")

# 6. Save
out = DATA_DIR / 'cotas.json'
with open(out, 'w', encoding='utf-8') as f:
    json.dump(cotas_dict, f, ensure_ascii=False)

size_mb = out.stat().st_size / 1024 / 1024
print(f"  Salvo: {out} ({size_mb:.1f} MB)")

# 7. Verify sample
sample_cnpj = next(iter(cotas_dict))
sample = cotas_dict[sample_cnpj]
print(f"  Amostra: {sample_cnpj}")
print(f"    {len(sample['datas'])} pontos: {sample['datas'][0]} → {sample['datas'][-1]}")
print(f"    Valores: {sample['valores'][0]:.2f} → {sample['valores'][-1]:.2f}")
print(f"\n✓ Pronto! Recarregue o dashboard.")
