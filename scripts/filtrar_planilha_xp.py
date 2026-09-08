#!/usr/bin/env python3
"""
Filtra uma exportação XP (combinada ou não) para conter APENAS os fundos que
já estão no dashboard — sem incluir fundos novos.

Uso:
    python scripts/filtrar_planilha_xp.py \
        --xp input/lista-fundos-08-09-2026.xlsx \
        --base data/fundos.json \
        --anterior caminho/planilha_anterior.xlsx \
        --out input/lista-fundos.xlsx

Regras:
  1. Mantém da exportação nova só as linhas cujo CNPJ está em --base
     (fundos.json ou fundos_rf.json). CNPJs novos são ignorados (listados no log).
  2. CNPJ da base que NÃO está na exportação nova é recuperado da planilha
     --anterior (linha antiga, com rentabilidades XP defasadas) — garante que
     nenhum fundo do dashboard (recomendados inclusive) desapareça.
  3. CNPJ duplicado na exportação (classes de cotas) → mantém a primeira linha.

A saída tem as mesmas 25 colunas da exportação XP, aba 'Fundos'.
"""

import argparse
import json
import sys
from pathlib import Path

import pandas as pd


def _ler_xp(caminho: str) -> pd.DataFrame:
    x = pd.ExcelFile(caminho)
    aba = next((a for a in ['Fundos', 'fundos', 'Planilha1', 'Sheet1'] if a in x.sheet_names), x.sheet_names[0])
    df = x.parse(aba, dtype=str)
    df.columns = [str(c).strip() for c in df.columns]
    if 'CNPJ_FUNDO' not in df.columns:
        sys.exit(f"✗ {caminho}: coluna CNPJ_FUNDO não encontrada. Colunas: {list(df.columns)}")
    df['CNPJ_FUNDO'] = df['CNPJ_FUNDO'].astype(str).str.strip()
    return df


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--xp', required=True, help='Exportação XP nova (.xlsx)')
    ap.add_argument('--base', required=True, help='fundos.json / fundos_rf.json com os CNPJs a manter')
    ap.add_argument('--anterior', default=None, help='Planilha XP anterior (fallback p/ CNPJs ausentes na nova)')
    ap.add_argument('--out', required=True, help='Planilha filtrada de saída (.xlsx)')
    args = ap.parse_args()

    base = json.loads(Path(args.base).read_text(encoding='utf-8'))
    cnpjs_base = [f['cnpj'].strip() for f in base]
    set_base = set(cnpjs_base)
    rec_base = {f['cnpj'].strip() for f in base if f.get('recomendado')}

    novo = _ler_xp(args.xp)
    n_linhas = len(novo)
    dup_mask = novo['CNPJ_FUNDO'].duplicated(keep='first')
    dup_na_base = sorted(set(novo.loc[dup_mask, 'CNPJ_FUNDO']) & set_base)
    novo = novo[~dup_mask]

    presentes = novo[novo['CNPJ_FUNDO'].isin(set_base)].copy()
    ausentes = [c for c in cnpjs_base if c not in set(presentes['CNPJ_FUNDO'])]
    ignorados = novo[~novo['CNPJ_FUNDO'].isin(set_base)]

    recuperados = pd.DataFrame(columns=novo.columns)
    nao_encontrados = list(ausentes)
    if ausentes and args.anterior:
        ant = _ler_xp(args.anterior)
        ant = ant[~ant['CNPJ_FUNDO'].duplicated(keep='first')]
        recuperados = ant[ant['CNPJ_FUNDO'].isin(set(ausentes))].copy()
        if list(recuperados.columns) != list(novo.columns):
            print(f"  ⚠ colunas diferem entre nova e anterior — alinhando pela nova")
            recuperados = recuperados.reindex(columns=novo.columns)
        nao_encontrados = [c for c in ausentes if c not in set(recuperados['CNPJ_FUNDO'])]

    saida = pd.concat([presentes, recuperados], ignore_index=True)
    # Ordena na ordem da base (estável, facilita diff visual)
    ordem = {c: i for i, c in enumerate(cnpjs_base)}
    saida = saida.sort_values('CNPJ_FUNDO', key=lambda s: s.map(ordem)).reset_index(drop=True)

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with pd.ExcelWriter(out, engine='openpyxl') as w:
        saida.to_excel(w, sheet_name='Fundos', index=False)

    nome = lambda df, c: (df.loc[df['CNPJ_FUNDO'] == c, 'NOME_FUNDO'].iloc[0][:48] if (df['CNPJ_FUNDO'] == c).any() else c)
    print(f"[filtrar_planilha_xp] {Path(args.xp).name} → {out}")
    print(f"  exportação nova:      {n_linhas} linhas, {novo['CNPJ_FUNDO'].nunique()} CNPJs únicos")
    print(f"  base ({Path(args.base).name}): {len(cnpjs_base)} fundos ({len(rec_base)} recomendados)")
    print(f"  ✓ atualizados pela XP nova:   {len(presentes)}")
    print(f"  ↺ recuperados da anterior:    {len(recuperados)}  (rentabilidades XP defasadas)")
    for c in recuperados['CNPJ_FUNDO']:
        print(f"      {'★ ' if c in rec_base else '  '}{nome(recuperados, c)}")
    if nao_encontrados:
        print(f"  ✗ NÃO encontrados em lugar nenhum: {len(nao_encontrados)}")
        for c in nao_encontrados:
            print(f"      {'★ ' if c in rec_base else '  '}{c}")
    if dup_na_base:
        print(f"  ⚠ CNPJs da base duplicados na exportação (1ª linha mantida): {dup_na_base}")
    print(f"  – novos ignorados (não estão na base): {len(ignorados)}")
    print(f"  → saída: {len(saida)} linhas | recomendados presentes: {len(rec_base & set(saida['CNPJ_FUNDO']))}/{len(rec_base)}")
    if len(saida) != len(cnpjs_base):
        print(f"  ⚠ saída ({len(saida)}) ≠ base ({len(cnpjs_base)})")
        sys.exit(2)


if __name__ == '__main__':
    main()
