"""
=============================================================================
ALPHA DESK — Pipeline de Dados
=============================================================================
Gera os arquivos JSON que alimentam o dashboard de fundos multimercado.

FONTES:
  1. Planilha XP          → identidade, classificações, rentabilidades, taxas
  2. CVM (inf_diario)     → PL, nº cotistas, série de cotas (para métricas quant)
  3. CVM (cad_fi)         → data de início do fundo
  4. BCB (API pública)    → série histórica do CDI
  5. IBGE (API pública)   → série histórica do IPCA
  6. ANBIMA (download)    → série histórica do IHFA
  7. recomendados.json    → lista curada manualmente pela equipe
  8. gestoras.json        → fichas das gestoras (manual)
  9. conteudo.json        → cartas e podcasts (manual)

OUTPUTS (pasta /data):
  fundos.json             → base principal de fundos com todas as métricas
  benchmarks.json         → séries históricas CDI, IPCA+X, IHFA
  gestoras.json           → fichas das gestoras (passthrough + enriquecido)
  conteudo.json           → cartas e podcasts (passthrough)
  meta.json               → metadata da última atualização

USO:
  pip install pandas openpyxl requests scipy numpy pyarrow
  python pipeline_fundos.py --xp lista-fundos.xlsx --output ./data

ATUALIZAÇÃO MENSAL:
  1. Baixe nova planilha da XP e salve como lista-fundos.xlsx
  2. Execute: python pipeline_fundos.py --xp lista-fundos.xlsx
  3. Copie a pasta /data para o dashboard
=============================================================================
"""

import argparse
import json
import os
import io
import re
import zipfile
import logging
from datetime import datetime, date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import requests

# ---------------------------------------------------------------------------
_METRICAS_TEMP = {}  # Armazenamento temporário de métricas entre etapas


# [SHARED-RF] Importado por scripts/pipeline_fundos_rf.py — não alterar interface.
class NumpyEncoder(json.JSONEncoder):
    """JSON encoder que converte tipos numpy para tipos nativos Python."""
    def default(self, obj):
        if isinstance(obj, (np.integer,)):
            return int(obj)
        if isinstance(obj, (np.floating,)):
            return float(obj)
        if isinstance(obj, (np.ndarray,)):
            return obj.tolist()
        if isinstance(obj, (np.bool_,)):
            return bool(obj)
        return super().default(obj)


def dedup_cotas_por_continuidade(df: pd.DataFrame, rescale_threshold: float = 0.10) -> pd.DataFrame:
    """Remove duplicatas de CNPJ+data mantendo a cota mais consistente com a série.

    A reforma CVM 2025 criou subclasses para um mesmo CNPJ, gerando múltiplas
    linhas por data com valores de cota diferentes (ex: subclasse nova começa ~1.0
    enquanto a série original está em ~2.85). O simples drop_duplicates(keep='first')
    pode pegar a subclasse errada, causando saltos de -65% nos gráficos.

    Estratégia em 2 passos:
    1. Para cada CNPJ com duplicatas, manter a cota mais próxima do valor do
       dia anterior (continuidade da série).
    2. Se no DIA do split estrutural (N_subclasses_hoje > N_subclasses_ontem) a
       cota escolhida ainda produzir um retorno diário absoluto > `rescale_threshold`
       (ex: 10%), rescala toda a série posterior por um fator que zera o degrau.
       Isso corrige casos onde nenhuma das subclasses disponíveis estava
       realmente próxima da cota pré-split (ex: Kadima Master II em 19/12/2025:
       pré 8.30, subclasses 1.43/6.38/5.36 — todas longe de 8.30).

    IMPORTANTE: Só rescala em "dias de split estrutural" (quando o número de
    subclasses aumenta) — assim evita falsos positivos em fundos voláteis que
    operam com N subclasses fixas e têm retornos diários reais > 10%.
    """
    if df.empty:
        return df

    dup_mask = df.duplicated(['CNPJ_NORM', 'DT_COMPTC'], keep=False)
    if not dup_mask.any():
        return df

    cnpjs_com_dup = df.loc[dup_mask, 'CNPJ_NORM'].unique()
    log.info(f"  Dedup continuidade: {len(cnpjs_com_dup)} CNPJs com datas duplicadas")

    # Separa: fundos sem duplicata (rápido) + fundos com duplicata (tratamento especial)
    df_ok = df[~df['CNPJ_NORM'].isin(cnpjs_com_dup)]
    df_dup = df[df['CNPJ_NORM'].isin(cnpjs_com_dup)]

    partes = [df_ok]
    rescales_log = []  # [(cnpj, data_split, retorno_original, fator)]

    for cnpj, grp in df_dup.groupby('CNPJ_NORM'):
        grp = grp.sort_values('DT_COMPTC').copy()
        indices_manter = []
        prev_val_scaled = None  # valor no "espaço rescalado"
        prev_n_subclasses = 0   # nº de linhas que o CNPJ tinha no dia anterior
        rescale_factor = 1.0    # fator cumulativo aplicado à série deste CNPJ
        picked_rows = []        # [(dt, idx, raw_val, factor)]

        for dt, dia_rows in grp.groupby('DT_COMPTC', sort=True):
            n_hoje = len(dia_rows)
            if n_hoje == 1:
                idx = dia_rows.index[0]
                raw_val = dia_rows.iloc[0]['VL_QUOTA']
            else:
                if prev_val_scaled is not None and pd.notna(prev_val_scaled):
                    # Compara subclasses no espaço rescalado
                    scaled_candidates = dia_rows['VL_QUOTA'] * rescale_factor
                    diffs = (scaled_candidates - prev_val_scaled).abs()
                    idx = diffs.idxmin()
                    raw_val = dia_rows.loc[idx, 'VL_QUOTA']
                    scaled_val = raw_val * rescale_factor
                    # Só checa rescale em "split estrutural": quando o nº de subclasses
                    # AUMENTA hoje em relação a ontem. Fundos que sempre têm N subclasses
                    # e movem juntos com retornos reais grandes NÃO são rescalados.
                    if (prev_val_scaled > 0
                            and n_hoje > prev_n_subclasses
                            and prev_n_subclasses >= 1):
                        ret = (scaled_val / prev_val_scaled) - 1
                        if abs(ret) > rescale_threshold:
                            # Rescala toda série posterior pra zerar o degrau
                            new_factor = prev_val_scaled / raw_val if raw_val else rescale_factor
                            rescales_log.append((cnpj, dt, ret, new_factor / rescale_factor))
                            rescale_factor = new_factor
                            scaled_val = prev_val_scaled  # zera o degrau
                else:
                    # Sem referência anterior: mantém a maior (série original)
                    idx = dia_rows['VL_QUOTA'].idxmax()
                    raw_val = dia_rows.loc[idx, 'VL_QUOTA']
                    scaled_val = raw_val * rescale_factor
            indices_manter.append(idx)
            picked_rows.append((dt, idx, raw_val, rescale_factor))
            prev_val_scaled = raw_val * rescale_factor
            prev_n_subclasses = n_hoje

        # Aplica o fator de rescale às linhas escolhidas (overwrite VL_QUOTA)
        sub = grp.loc[indices_manter].copy()
        # Constrói série de fatores alinhada com sub.index
        factor_map = {idx: factor for _, idx, _, factor in picked_rows}
        sub['VL_QUOTA'] = sub.index.map(factor_map).to_series(index=sub.index) * sub['VL_QUOTA']
        partes.append(sub)

    result = pd.concat(partes, ignore_index=True)
    result = result.sort_values(['CNPJ_NORM', 'DT_COMPTC']).reset_index(drop=True)

    if rescales_log:
        log.warning(f"  Dedup rescale: {len(rescales_log)} degraus > {rescale_threshold*100:.1f}% corrigidos")
        for cnpj, dt, ret, factor in rescales_log:
            log.warning(f"    {cnpj} @ {dt}: retorno original {ret*100:+.2f}% → fator {factor:.4f}")
    log.info(f"  Dedup continuidade: {len(df)} → {len(result)} registros")
    return result

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S"
)
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# CONFIGURAÇÕES
# ---------------------------------------------------------------------------

# Categorias XP a EXCLUIR do dashboard (não são multimercado de fato)
CLASSES_XP_EXCLUIR_PARCIAL = [
    'Alternativo',
    'Cambial',
    'Crédito',
    'Credito',
    'Debêntures',
    'Debentures',
    'Internacional Renda Fixa',
    'Renda Fixa Ativo',
    'Renda Fixa Inflação',
    'Renda Fixa Inflacao',
]

def _classe_excluida(class_xp: str) -> bool:
    """Retorna True se a classe XP deve ser excluída do dashboard."""
    if not class_xp:
        return False
    lower = class_xp.lower()
    return any(pat.lower() in lower for pat in CLASSES_XP_EXCLUIR_PARCIAL)

# Quantos meses de histórico de cotas baixar da CVM
# 36 meses = cobre retorno 24M + janela para calcular métricas de 24M
MESES_HISTORICO = 36

# Mínimo de dias úteis para calcular métricas (evita fundos muito novos)
MIN_DIAS_CALCULO = 63   # ~3 meses

# Parâmetros das métricas
DIAS_UTEIS_ANO = 252
PERCENTIL_VAR   = 5     # VaR 95% = percentil 5 dos retornos diários

# Taxa livre de risco para Sharpe/Sortino: usamos CDI diário calculado dinamicamente
# (não fixo, para refletir o período analisado)

# IPCA targets para benchmarks
IPCA_SPREADS = {
    "IPCA+3.5%": 0.035,
    "IPCA+5%":   0.05,
    "IPCA+6%":   0.06,
}


# ---------------------------------------------------------------------------
# ETAPA 1 — Leitura e parsing da planilha XP
# [SHARED-RF] parse_pct, parse_float, tipo_investidor — importadas pelo
#             scripts/pipeline_fundos_rf.py. Alterar assinatura quebra o RF.
# ---------------------------------------------------------------------------

def parse_pct(val) -> float | None:
    """
    Converte percentual para decimal: '18,65%' → 0.1865, '-1,09%' → -0.01090
    Regra: strings com % sempre dividem por 100 (remove ambiguidade do heurístico).
    Floats já em decimal (ex: 0.1865) retornam direto.
    """
    if val is None:
        return None
    if isinstance(val, (int, float)):
        # Vindo como float do pandas: já deve ser decimal (0.1865)
        # Mas alguns campos XP entregam como 18.65 — heurística pelo tamanho
        v = float(val)
        return round(v / 100, 8) if abs(v) > 2 else round(v, 8)
    # String: SEMPRE tem o símbolo % ou veio formatado como percentual → divide por 100
    s = str(val).strip().replace('%', '').replace(',', '.').replace(' ', '')
    try:
        return round(float(s) / 100, 8)
    except ValueError:
        return None


def parse_float(val) -> float | None:
    if val is None:
        return None
    if isinstance(val, (int, float)):
        return float(val)
    try:
        return float(str(val).replace(',', '.').strip())
    except ValueError:
        return None


def tipo_investidor(val) -> str:
    """Blank ou NaN → 'Geral' (aberto para todos os tipos de investidores)."""
    if val is None:
        return 'Geral'
    s = str(val).strip()
    if s == '' or s.lower() == 'nan':
        return 'Geral'
    return s


def ler_planilha_xp(caminho: str) -> pd.DataFrame:
    log.info(f"Lendo planilha XP: {caminho}")
    # Tenta nomes de aba que a XP já usou
    import openpyxl
    wb_temp = openpyxl.load_workbook(caminho, read_only=True)
    abas = wb_temp.sheetnames
    wb_temp.close()
    aba_alvo = None
    for nome in ['Fundos', 'fundos', 'Planilha1', 'Sheet1']:
        if nome in abas:
            aba_alvo = nome
            break
    if aba_alvo is None:
        aba_alvo = abas[0]  # fallback: primeira aba
    log.info(f"  Aba utilizada: '{aba_alvo}' (disponíveis: {abas})")
    wb_df = pd.read_excel(caminho, sheet_name=aba_alvo, dtype=str)

    # Normaliza nomes de colunas
    wb_df.columns = [c.strip().upper().replace(' ', '_') for c in wb_df.columns]

    fundos = []
    for _, row in wb_df.iterrows():
        # Filtro: apenas CVM = Multimercado
        if row.get('CLASSIFICAÇÃO_CVM', '').strip() != 'Multimercado':
            continue

        class_xp = row.get('CLASSIFICAÇÃO_XP', '')
        class_xp = str(class_xp).strip() if class_xp and str(class_xp) != 'nan' else ''
        class_xp = class_xp or 'Não classificado'

        # Filtro: exclui categorias que não são multimercado de fato
        if _classe_excluida(class_xp):
            continue

        f = {
            # — Identidade
            "nome":               row.get('NOME_FUNDO', '').strip(),
            "cnpj":               row.get('CNPJ_FUNDO', '').strip(),
            "gestora":            row.get('NOME_GESTORA', '').strip(),
            # — Classificações
            "class_cvm":          'Multimercado',
            "class_xp":           class_xp,
            # — Acesso
            "captacao_aberta":    row.get('CAPTAÇÃO', '').strip() == 'Aberta',
            "tipo_investidor":    tipo_investidor(row.get('TIPO_INVESTIDOR')),
            "aplicacao_minima":   parse_float(row.get('APLICAÇÃO_INICIAL_MÍNIMA')),
            "movimentacao_minima":parse_float(row.get('MOVIMENTAÇÃO_MÍNIMA')),
            # — Liquidez
            "cotizacao_resgate":  row.get('COTIZAÇÃO_RESGATE', '').strip(),
            "periodo_cotizacao":  row.get('PERÍODO_COTIZAÇÃO', '').strip(),
            "liquidacao_resgate": row.get('LIQUIDAÇÃO_RESGATE', '').strip(),
            "periodo_liquidacao": row.get('PERÍODO_LIQUIDAÇÃO', '').strip(),
            # — Rentabilidades prontas (XP calcula)
            "rent_dia":  parse_pct(row.get('RENTABILIDADE_DIA')),
            "rent_mes":  parse_pct(row.get('RENTABILIDADE_MÊS')),
            "rent_ano":  parse_pct(row.get('RENTABILIDADE_ANO')),
            "rent_12m":  parse_pct(row.get('RENTABILIDADE_12M')),
            "rent_24m":  parse_pct(row.get('RENTABILIDADE_24M')),
            "rent_36m":  parse_pct(row.get('RENTABILIDADE_36M')),
            # — Cota
            "valor_cota": parse_float(row.get('VALOR_COTA')),
            "data_cota":  row.get('DATA_COTA', '').strip(),
            # — Risco Gênio (score interno XP, 1-100)
            "risco_genio": int(float(row['RISCO_GÊNIO'])) if row.get('RISCO_GÊNIO') else None,
            # — Taxas (vêm como percentual anual, ex: 2.0 = 2% a.a.)
            "taxa_adm":     parse_float(row.get('TAXA_ADMINISTRAÇÃO')),
            "taxa_adm_max": parse_float(row.get('TAXA_ADMINISTRAÇÃO_MÁXIMA')),
            "taxa_perf":    parse_float(row.get('TAXA_PERFORMANCE')),
            # — Campos a enriquecer via CVM
            "pl":           None,
            "num_cotistas": None,
            "data_inicio":  None,
            # — Métricas calculadas
            "sharpe":            None,
            "sharpe_24m":        None,
            "sharpe_36m":        None,
            "sortino":           None,
            "sortino_24m":       None,
            "sortino_36m":       None,
            "volatilidade":      None,
            "volatilidade_24m":  None,
            "volatilidade_36m":  None,
            "drawdown_max":      None,
            "drawdown_max_36m":  None,
            "consistencia":      None,
            "consistencia_36m":  None,
            "corr_cdi_36m":      None,
            "var_95":            None,
            "calmar":            None,
            "meses_pos":      None,
            "total_meses":    None,
            "variacao_pl_12m": None,  # captação líquida: variação % do PL em 12M
            "pl_12m_atras":   None,
            # — Campos manuais (editados em recomendados.json)
            "longevidade_anos": None,
            "recomendado":  False,
            "aprovado":     False,
            # — Grupo de volatilidade (derivado de class_xp)
            "grupo_vol":    _grupo_vol(class_xp),
        }
        fundos.append(f)

    df = pd.DataFrame(fundos)
    log.info(f"  → {len(df)} fundos multimercado carregados")
    log.info(f"  → Captação aberta: {df['captacao_aberta'].sum()} | Fechada: {(~df['captacao_aberta']).sum()}")
    return df


def _grupo_vol(class_xp: str) -> str:
    """Deriva grupo de volatilidade a partir da classificação XP."""
    mapping = {
        'Macro Alta Vol': 'Alta',
        'Macro Média Vol': 'Média',
        'Macro Baixa Vol': 'Baixa',
        'Long Short Neutro': 'Baixa',
        'Long Short Direcional': 'Média',
        'Quantitativo': 'Baixa',
        'Multiestratégia': 'Média',
        'Alternativo Líquido': 'Alta',
        'Internacional Multimercado Hedgeado': 'Alta',
        'Internacional Multimercado Não Hedgeado': 'Alta',
        'Internacional Macro Hedgeado': 'Alta',
    }
    return mapping.get(class_xp, 'N/A')


# ---------------------------------------------------------------------------
# ETAPA 2 — CVM: cadastro de fundos (data de início)
# [SHARED-RF] baixar_cadastro_cvm, enriquecer_cadastro — importadas pelo
#             scripts/pipeline_fundos_rf.py. Alterar assinatura quebra o RF.
# ---------------------------------------------------------------------------

CVM_CAD_URL = "https://dados.cvm.gov.br/dados/FI/CAD/DADOS/cad_fi.csv"

def baixar_cadastro_cvm() -> pd.DataFrame:
    log.info("Baixando cadastro CVM (cad_fi.csv)...")
    try:
        r = requests.get(CVM_CAD_URL, timeout=60)
        r.raise_for_status()

        # Lê sem filtrar colunas para detectar o formato atual
        df_raw = pd.read_csv(
            io.StringIO(r.content.decode('latin-1')),
            sep=';', dtype=str, nrows=5,
        )
        colunas = list(df_raw.columns)

        # Mapeia nomes alternativos — CVM renomeou colunas em 2025
        col_cnpj  = next((c for c in colunas if 'CNPJ' in c.upper()), None)
        col_data  = next((c for c in colunas if 'DT_INI' in c.upper() or 'DT_CONSTIT' in c.upper() or 'DATA_INI' in c.upper()), None)
        col_nome  = next((c for c in colunas if 'DENOM' in c.upper() or 'NM_FUNDO' in c.upper() or 'NOME' in c.upper()), None)

        if not col_cnpj or not col_data:
            log.warning(f"  ✗ Colunas CNPJ/Data não encontradas no cad_fi. Disponíveis: {colunas[:10]}")
            return pd.DataFrame()

        cols_ler = [c for c in [col_cnpj, col_data, col_nome] if c]
        df = pd.read_csv(
            io.StringIO(r.content.decode('latin-1')),
            sep=';', usecols=cols_ler, dtype=str,
        )

        # Padroniza nomes
        df = df.rename(columns={col_cnpj: 'CNPJ_FUNDO', col_data: 'DT_INI_ATIV'})
        if col_nome: df = df.rename(columns={col_nome: 'DENOM_SOCIAL'})

        df['CNPJ_FUNDO'] = df['CNPJ_FUNDO'].str.strip()
        df['DT_INI_ATIV'] = pd.to_datetime(df['DT_INI_ATIV'], errors='coerce')

        # Indexa tanto pelo CNPJ original quanto normalizado (sem pontuação)
        # para garantir match independente do formato da planilha XP
        df_norm = df.copy()
        df_norm['CNPJ_FUNDO'] = df_norm['CNPJ_FUNDO'].str.replace(r'[./-]', '', regex=True)
        df_combined = pd.concat([df, df_norm]).drop_duplicates(subset='CNPJ_FUNDO')

        log.info(f"  → {len(df)} fundos no cadastro CVM (col CNPJ: {col_cnpj}, col data: {col_data})")
        return df_combined.set_index('CNPJ_FUNDO')
    except Exception as e:
        log.warning(f"  ✗ Falha ao baixar cadastro CVM: {e}")
        return pd.DataFrame()


def enriquecer_cadastro(df_fundos: pd.DataFrame, df_cad: pd.DataFrame) -> pd.DataFrame:
    if df_cad.empty:
        log.warning("  Cadastro CVM vazio, pulando enriquecimento de data de início")
        return df_fundos

    def get_inicio(cnpj):
        # CVM cadastro usa CNPJ sem pontuação (ex: 40187334000130)
        cnpj_clean = cnpj.replace('.', '').replace('/', '').replace('-', '').strip()
        cnpj_orig  = cnpj.strip()
        for fmt in [cnpj_clean, cnpj_orig]:
            try:
                if fmt in df_cad.index:
                    row = df_cad.loc[fmt]
                    # loc pode retornar DataFrame se duplicado — pegar primeira linha
                    if isinstance(row, pd.DataFrame):
                        row = row.iloc[0]
                    dt = row['DT_INI_ATIV']
                    if pd.notna(dt):
                        return dt.strftime('%Y-%m-%d')
            except Exception:
                continue
        return None

    df_fundos['data_inicio'] = df_fundos['cnpj'].apply(get_inicio)
    preenchidos = df_fundos['data_inicio'].notna().sum()
    log.info(f"  → Data de início preenchida: {preenchidos}/{len(df_fundos)} fundos")
    return df_fundos


# ---------------------------------------------------------------------------
# ETAPA 3 — CVM: informes diários (PL, cotistas, série de cotas)
# [SHARED-RF] _normalizar_cnpj, baixar_informes_cvm — importadas pelo
#             scripts/pipeline_fundos_rf.py. Alterar assinatura quebra o RF.
# ---------------------------------------------------------------------------

CVM_INF_BASE = "https://dados.cvm.gov.br/dados/FI/DOC/INF_DIARIO/DADOS/"


def _normalizar_cnpj(cnpj: str) -> str:
    return cnpj.replace('.', '').replace('/', '').replace('-', '')


def _meses_range(n_meses: int) -> list[tuple[int, int]]:
    """Retorna lista de (ano, mes) dos últimos n_meses, mais recente primeiro."""
    hoje = date.today()
    meses = []
    for i in range(n_meses):
        mes = (hoje.month - 1 - i) % 12 + 1
        ano = hoje.year + ((hoje.month - 1 - i) // 12)
        meses.append((ano, mes))
    return meses


def baixar_informes_cvm(cnpjs: set[str], n_meses: int = MESES_HISTORICO,
                        pasta_cache: Path = None) -> pd.DataFrame:
    """
    Baixa informes diários da CVM filtrando APENAS os CNPJs da planilha XP.

    Cache local (pasta data/cache_cvm):
    - Meses já baixados são lidos do disco — sem re-download
    - Só o mês atual é sempre re-baixado (pode estar incompleto)
    - Execuções mensais: de 40min → ~2min
    """
    cnpjs_norm = {_normalizar_cnpj(c) for c in cnpjs}

    if pasta_cache is None:
        pasta_cache = Path('./data/cache_cvm')
    pasta_cache.mkdir(parents=True, exist_ok=True)

    meses = _meses_range(n_meses)
    hoje = date.today()
    mes_atual = (hoje.year, hoje.month)

    log.info(f"Baixando informes CVM — {n_meses} meses — {len(cnpjs)} fundos da planilha XP")
    log.info(f"  Cache: {pasta_cache.resolve()}")

    frames = []
    baixados = 0
    do_cache = 0

    for ano, mes in meses:
        nome_zip   = f"inf_diario_fi_{ano}{mes:02d}.zip"
        nome_cache = f"cotas_xp_{ano}{mes:02d}.parquet"
        path_cache = pasta_cache / nome_cache
        eh_mes_atual = (ano, mes) == mes_atual

        # Lê do cache se já existe (exceto mês atual)
        if path_cache.exists() and not eh_mes_atual:
            try:
                chunk = pd.read_parquet(path_cache)
                frames.append(chunk)
                do_cache += 1
                log.info(f"  📂 {nome_zip}: {len(chunk)} registros (cache local)")
                continue
            except Exception:
                pass  # cache corrompido → re-baixa

        # Download do ZIP
        url = CVM_INF_BASE + nome_zip
        try:
            r = requests.get(url, timeout=180)
            if r.status_code == 404:
                log.debug(f"  {nome_zip}: ainda não publicado, pulando")
                continue
            r.raise_for_status()
            baixados += 1

            with zipfile.ZipFile(io.BytesIO(r.content)) as z:
                csv_name = z.namelist()[0]
                with z.open(csv_name) as f:
                    # Lê cabeçalho primeiro para mapear nomes de colunas
                    # (CVM renomeou colunas em 2025: TP_FUNDO_CLASSE → novo formato)
                    header_df = pd.read_csv(f, sep=';', nrows=0, encoding='latin-1')
                    colunas_disponiveis = list(header_df.columns)

                # Mapeia nomes alternativos que a CVM já usou
                # Mapeamento atualizado conforme diagnóstico de fev/2026:
                # Novo formato CVM (2025+): CNPJ_FUNDO_CLASSE, DT_COMPTC, VL_QUOTA, VL_PATRIM_LIQ, NR_COTST
                # Formato antigo CVM (até 2024): CNPJ_FUNDO, DT_COMPTC, VL_QUOTA, VL_PATRIM_LIQ, NR_COTST
                mapa_colunas = {
                    'cnpj':   ['CNPJ_FUNDO_CLASSE', 'CNPJ_FUNDO', 'NR_CNPJ_FUNDO'],
                    'data':   ['DT_COMPTC', 'DT_COMPTC_MVMT', 'DT_MVMT'],
                    'cota':   ['VL_QUOTA', 'VL_COTA', 'VL_QUOTA_FUNDO'],
                    'pl':     ['VL_PATRIM_LIQ', 'VL_PATRIM_LIQ_FUNDO', 'VL_PL'],
                    'cotist': ['NR_COTST', 'NR_COTST_FUNDO', 'QT_COTST'],
                }

                def achar_coluna(opcoes):
                    for nome in opcoes:
                        if nome in colunas_disponiveis:
                            return nome
                    return None

                col_cnpj   = achar_coluna(mapa_colunas['cnpj'])
                col_data   = achar_coluna(mapa_colunas['data'])
                col_cota   = achar_coluna(mapa_colunas['cota'])
                col_pl     = achar_coluna(mapa_colunas['pl'])
                col_cotist = achar_coluna(mapa_colunas['cotist'])

                colunas_faltando = [k for k, v in {
                    'CNPJ': col_cnpj, 'DATA': col_data, 'COTA': col_cota
                }.items() if v is None]

                if colunas_faltando:
                    log.warning(f"  ✗ {nome_zip}: colunas não encontradas: {colunas_faltando}")
                    log.warning(f"    Colunas disponíveis: {colunas_disponiveis[:10]}...")
                    continue

                cols_ler = [c for c in [col_cnpj, col_data, col_cota, col_pl, col_cotist] if c]

                with zipfile.ZipFile(io.BytesIO(r.content)) as z:
                    with z.open(z.namelist()[0]) as f:
                        chunk = pd.read_csv(
                            f, sep=';',
                            dtype={col_cnpj: str},
                            usecols=cols_ler,
                            encoding='latin-1',
                        )

                # Padroniza nomes para o resto do script
                chunk = chunk.rename(columns={
                    col_cnpj:   'CNPJ_FUNDO',
                    col_data:   'DT_COMPTC',
                    col_cota:   'VL_QUOTA',
                    col_pl:     'VL_PATRIM_LIQ',
                    col_cotist: 'NR_COTST',
                })

            # Filtra só os CNPJs da planilha XP (reduz ~500k → ~3k linhas por mês)
            chunk['CNPJ_NORM'] = chunk['CNPJ_FUNDO'].str.replace(r'[./-]', '', regex=True)
            chunk = chunk[chunk['CNPJ_NORM'].isin(cnpjs_norm)].copy()

            chunk['DT_COMPTC'] = pd.to_datetime(chunk['DT_COMPTC'], errors='coerce')

            # Novo formato CVM (2025+): colunas já vêm como float — não usar .str
            # Formato antigo CVM (até 2024): colunas vêm como string com vírgula decimal
            def to_num(col):
                if col.dtype == object:
                    return pd.to_numeric(col.str.replace(',', '.', regex=False), errors='coerce')
                return pd.to_numeric(col, errors='coerce')

            chunk['VL_QUOTA']      = to_num(chunk['VL_QUOTA'])
            chunk['VL_PATRIM_LIQ'] = to_num(chunk['VL_PATRIM_LIQ'])
            chunk['NR_COTST']      = to_num(chunk['NR_COTST'])

            if not chunk.empty:
                if not eh_mes_atual:
                    chunk.to_parquet(path_cache, index=False)  # salva cache filtrado
                frames.append(chunk)
                log.info(f"  ✓ {nome_zip}: {len(chunk)} registros — {chunk['CNPJ_NORM'].nunique()} fundos")
            else:
                log.debug(f"  {nome_zip}: nenhum dos CNPJs encontrado")

        except Exception as e:
            log.warning(f"  ✗ Falha em {nome_zip}: {e}")

    log.info(f"  → {baixados} baixados da CVM | {do_cache} lidos do cache")

    if not frames:
        log.warning("  Nenhum informe CVM disponível")
        return pd.DataFrame()

    df = pd.concat(frames, ignore_index=True)
    df = df.sort_values(['CNPJ_NORM', 'DT_COMPTC'])
    df = dedup_cotas_por_continuidade(df)
    log.info(f"  → Total: {len(df)} registros | {df['CNPJ_NORM'].nunique()} fundos com dados")
    return df


# ---------------------------------------------------------------------------
# ETAPA 4 — Cálculo de métricas quantitativas
# [SHARED-RF] calcular_retornos_diarios, calcular_volatilidade, calcular_sharpe,
#             calcular_sortino, calcular_drawdown_max, calcular_var_95,
#             calcular_calmar — importadas pelo scripts/pipeline_fundos_rf.py.
#             Alterar assinatura quebra o RF.
# ---------------------------------------------------------------------------

def calcular_retorno_acumulado(cotas: pd.Series, janela_dias: int) -> float | None:
    """Retorno acumulado nos últimos `janela_dias` dias úteis."""
    if len(cotas) < 2:
        return None
    serie = cotas.dropna()
    if len(serie) < janela_dias:
        return None
    return float(serie.iloc[-1] / serie.iloc[-janela_dias] - 1)


def calcular_retornos_diarios(cotas: pd.Series) -> pd.Series:
    return cotas.pct_change().dropna()


def calcular_volatilidade(retornos: pd.Series, janela: int = DIAS_UTEIS_ANO) -> float | None:
    """Volatilidade anualizada (desvio padrão dos retornos diários × √252)."""
    if len(retornos) < MIN_DIAS_CALCULO:
        return None
    r = retornos.tail(janela) if len(retornos) > janela else retornos
    return float(r.std() * np.sqrt(DIAS_UTEIS_ANO))


def calcular_sharpe(retornos: pd.Series, retornos_cdi: pd.Series, janela: int = DIAS_UTEIS_ANO) -> float | None:
    """Sharpe = (retorno médio diário - CDI médio diário) / vol diária × √252."""
    if len(retornos) < MIN_DIAS_CALCULO:
        return None
    r = retornos.tail(janela)
    cdi = retornos_cdi.reindex(r.index).dropna()
    r_alinhado = r.reindex(cdi.index).dropna()
    if len(r_alinhado) < MIN_DIAS_CALCULO:
        return None
    excesso = r_alinhado - cdi
    vol = r_alinhado.std()
    if vol == 0:
        return None
    return float((excesso.mean() / vol) * np.sqrt(DIAS_UTEIS_ANO))


def calcular_sortino(retornos: pd.Series, retornos_cdi: pd.Series, janela: int = DIAS_UTEIS_ANO) -> float | None:
    """Sortino = (retorno médio - CDI médio) / downside deviation × √252."""
    if len(retornos) < MIN_DIAS_CALCULO:
        return None
    r = retornos.tail(janela)
    cdi = retornos_cdi.reindex(r.index).dropna()
    r_alinhado = r.reindex(cdi.index).dropna()
    if len(r_alinhado) < MIN_DIAS_CALCULO:
        return None
    excesso = r_alinhado - cdi
    downside = excesso[excesso < 0]
    if len(downside) < 5:
        return None
    downside_dev = downside.std()
    if downside_dev == 0:
        return None
    return float((excesso.mean() / downside_dev) * np.sqrt(DIAS_UTEIS_ANO))


def calcular_drawdown_max(cotas: pd.Series) -> float | None:
    """Maior queda do pico ao vale — retorna valor POSITIVO (ex: 0.15 = 15% de queda)."""
    if len(cotas) < 2:
        return None
    rolling_max = cotas.cummax()
    drawdowns = (cotas - rolling_max) / rolling_max
    dd_max = drawdowns.min()
    # Return absolute value so 0.15 = 15% drawdown (smaller = better, dir='lower')
    return abs(float(dd_max)) if pd.notna(dd_max) else None


def calcular_var_95(retornos: pd.Series, janela: int = DIAS_UTEIS_ANO) -> float | None:
    """VaR 95% histórico: percentil 5 dos retornos diários."""
    if len(retornos) < MIN_DIAS_CALCULO:
        return None
    r = retornos.tail(janela)
    return float(np.percentile(r.dropna(), PERCENTIL_VAR))


def calcular_calmar(ret_12m: float | None, drawdown_max: float | None) -> float | None:
    """Calmar = retorno 12M / |drawdown máximo|."""
    if ret_12m is None or drawdown_max is None or drawdown_max == 0:
        return None
    return round(ret_12m / abs(drawdown_max), 4)


def enriquecer_metricas(df_fundos: pd.DataFrame, df_cotas: pd.DataFrame) -> pd.DataFrame:
    """
    Para cada fundo, calcula as métricas quant a partir da série de cotas CVM.
    Também extrai PL e número de cotistas mais recentes.
    """
    if df_cotas.empty:
        log.warning("  Sem dados de cotas CVM — métricas quant não calculadas")
        return df_fundos

    # Série do CDI diário (vem do benchmarks, calculada à parte)
    # Aqui usamos um placeholder; será substituído depois que baixarmos o CDI
    # Ver função `calcular_benchmarks` abaixo
    log.info("Calculando métricas quantitativas via cotas CVM...")

    def norm_cnpj(cnpj: str) -> str:
        return cnpj.replace('.', '').replace('/', '').replace('-', '')

    # Indexa cotas por CNPJ normalizado
    df_cotas = df_cotas.copy()
    df_cotas['CNPJ_NORM'] = df_cotas['CNPJ_FUNDO'].str.replace(r'[./-]', '', regex=True)

    metricas = {}
    for cnpj_orig, grupo in df_cotas.groupby('CNPJ_NORM'):
        grupo = grupo.sort_values('DT_COMPTC').drop_duplicates('DT_COMPTC', keep='last').set_index('DT_COMPTC')
        cotas = grupo['VL_QUOTA'].dropna()

        if len(cotas) < 5:
            continue

        ret = calcular_retornos_diarios(cotas)

        # PL e cotistas mais recentes
        pl_recente = grupo['VL_PATRIM_LIQ'].dropna().iloc[-1] if not grupo['VL_PATRIM_LIQ'].dropna().empty else None
        cotistas_recente = grupo['NR_COTST'].dropna().iloc[-1] if not grupo['NR_COTST'].dropna().empty else None

        # Variação de PL: PL atual vs PL há ~252 dias úteis (12M)
        pl_serie = grupo['VL_PATRIM_LIQ'].dropna()
        pl_atual = float(pl_recente) / 1e6 if pl_recente else None
        pl_12m_atras = None
        variacao_pl_12m = None
        if len(pl_serie) >= DIAS_UTEIS_ANO:
            pl_12m_atras = float(pl_serie.iloc[-DIAS_UTEIS_ANO]) / 1e6
            if pl_12m_atras and pl_12m_atras > 0:
                variacao_pl_12m = round((pl_atual - pl_12m_atras) / pl_12m_atras, 6)

        # Consistência: % de meses com retorno positivo
        ret_mensal = (1 + ret).resample('ME').prod() - 1
        meses_pos = int((ret_mensal > 0).sum())
        total_meses = len(ret_mensal)
        consistencia = round(meses_pos / total_meses, 4) if total_meses > 0 else None

        # ── Janelas fixas para análise quant ──
        JANELA_36M = DIAS_UTEIS_ANO * 3
        ret_36m = ret.tail(JANELA_36M)
        cotas_36m = cotas.tail(JANELA_36M)

        # Consistência 36M
        ret_mensal_36m = (1 + ret_36m).resample('ME').prod() - 1
        meses_pos_36m  = int((ret_mensal_36m > 0).sum())
        total_meses_36m = len(ret_mensal_36m)
        consistencia_36m = round(meses_pos_36m / total_meses_36m, 4) if total_meses_36m > 0 else None

        metricas[cnpj_orig] = {
            'pl':               round(pl_atual, 2) if pl_atual else None,
            'pl_12m_atras':     round(pl_12m_atras, 2) if pl_12m_atras else None,
            'variacao_pl_12m':  variacao_pl_12m,
            'num_cotistas':     int(cotistas_recente) if cotistas_recente else None,
            'volatilidade':     round(calcular_volatilidade(ret) or 0, 6),
            'volatilidade_24m': round(calcular_volatilidade(ret, janela=DIAS_UTEIS_ANO * 2) or 0, 6),
            'volatilidade_36m': round(calcular_volatilidade(ret_36m) or 0, 6),
            'drawdown_max':     round(calcular_drawdown_max(cotas) or 0, 6),
            'drawdown_max_36m': round(calcular_drawdown_max(cotas_36m) or 0, 6),
            'var_95':           round(calcular_var_95(ret) or 0, 6),
            'consistencia':     consistencia,
            'consistencia_36m': consistencia_36m,
            'meses_pos':        meses_pos,
            '_ret':             ret,   # série completa para segunda passagem
            'total_meses':    total_meses,
            '_ret':           ret,
            '_cotas':         cotas,
        }

    log.info(f"  → Métricas calculadas para {len(metricas)} fundos")

    # Aplica no DataFrame
    def aplicar(row):
        cnpj_n = norm_cnpj(row['cnpj'])
        m = metricas.get(cnpj_n, {})
        for campo in ['pl', 'pl_12m_atras', 'variacao_pl_12m', 'num_cotistas', 'volatilidade', 'volatilidade_24m', 'volatilidade_36m', 'drawdown_max', 'drawdown_max_36m', 'var_95', 'consistencia', 'consistencia_36m', 'corr_cdi_36m', 'meses_pos', 'total_meses', 'longevidade_anos']:
            if campo in m:
                row[campo] = m[campo]
        row['calmar'] = calcular_calmar(row.get('rent_12m'), row.get('drawdown_max'))
        # Longevidade em anos a partir dos dados CVM (mais preciso que cad_fi)
        m = metricas.get(_normalizar_cnpj(row['cnpj']), {})
        cotas = m.get('_cotas')
        if cotas is not None and len(cotas) > 0:
            dias_hist = len(cotas)
            row['longevidade_anos'] = round(dias_hist / DIAS_UTEIS_ANO, 2)
        return row

    df_fundos = df_fundos.apply(aplicar, axis=1)

    # Guarda metricas (com _ret e _cotas) para segunda passagem com CDI
    return df_fundos, metricas


def segunda_passagem_sharpe(df_fundos: pd.DataFrame, serie_cdi: pd.Series, metricas: dict = None) -> pd.DataFrame:
    """Segunda passagem: calcula Sharpe e Sortino agora que temos o CDI."""
    if metricas is None:
        metricas = {}
    if not metricas or serie_cdi.empty:
        return df_fundos

    def norm_cnpj(cnpj: str) -> str:
        return cnpj.replace('.', '').replace('/', '').replace('-', '')

    def aplicar(row):
        cnpj_n = norm_cnpj(row['cnpj'])
        m = metricas.get(cnpj_n, {})
        ret = m.get('_ret')
        if ret is not None and not ret.empty:
            J1 = DIAS_UTEIS_ANO
            J2 = DIAS_UTEIS_ANO * 2
            J3 = DIAS_UTEIS_ANO * 3
            row['sharpe']      = round(calcular_sharpe(ret, serie_cdi, janela=J1) or 0, 4)
            row['sharpe_24m']  = round(calcular_sharpe(ret, serie_cdi, janela=J2) or 0, 4)
            row['sharpe_36m']  = round(calcular_sharpe(ret, serie_cdi, janela=J3) or 0, 4)
            row['sortino']     = round(calcular_sortino(ret, serie_cdi, janela=J1) or 0, 4)
            row['sortino_24m'] = round(calcular_sortino(ret, serie_cdi, janela=J2) or 0, 4)
            row['sortino_36m'] = round(calcular_sortino(ret, serie_cdi, janela=J3) or 0, 4)
            # Correlação com CDI (36M): mede quanto o retorno é explicado pelo CDI
            # Valor baixo = retorno genuinamente não-correlacionado = alfa real
            ret_36m = ret.tail(J3)
            cdi_36m = serie_cdi.reindex(ret_36m.index).dropna()
            ret_alinhado = ret_36m.reindex(cdi_36m.index).dropna()
            if len(ret_alinhado) >= 60:
                row['corr_cdi_36m'] = round(float(ret_alinhado.corr(cdi_36m)), 4)
            else:
                row['corr_cdi_36m'] = None
        return row

    df_fundos = df_fundos.apply(aplicar, axis=1)
    return df_fundos


# ---------------------------------------------------------------------------
# ETAPA 5 — Benchmarks: CDI, IPCA, IHFA
# [SHARED-RF] baixar_cdi, baixar_ipca, baixar_ihfa, _parsear_csv_ihfa,
#             construir_ipca_mais_spread, _serie_para_dict, calcular_acumulado
#             — importadas pelo scripts/pipeline_fundos_rf.py.
#             Alterar assinatura quebra o RF.
# ---------------------------------------------------------------------------

# BCB API — série 12 = CDI diário
BCB_CDI_URL   = "https://api.bcb.gov.br/dados/serie/bcdata.sgs.12/dados?formato=json&dataInicial={}&dataFinal={}"
# BCB API — série 433 = IPCA mensal
BCB_IPCA_URL  = "https://api.bcb.gov.br/dados/serie/bcdata.sgs.433/dados?formato=json&dataInicial={}&dataFinal={}"
# ANBIMA IHFA — download direto
ANBIMA_IHFA_URL = "https://www.anbima.com.br/informacoes/fundos/resultado-de-indices/arqs/ihfa_diario.csv"


def baixar_cdi(anos: int = 5) -> pd.Series:
    """Retorna série diária do CDI (retorno decimal diário)."""
    log.info("Baixando CDI (BCB)...")
    inicio = (date.today() - timedelta(days=anos * 365)).strftime('%d/%m/%Y')
    fim = date.today().strftime('%d/%m/%Y')
    try:
        r = requests.get(BCB_CDI_URL.format(inicio, fim), timeout=30)
        r.raise_for_status()
        data = r.json()
        df = pd.DataFrame(data)
        df['data'] = pd.to_datetime(df['data'], dayfirst=True)
        df['valor'] = pd.to_numeric(df['valor'], errors='coerce') / 100  # % → decimal
        df = df.set_index('data')['valor'].dropna().sort_index()
        log.info(f"  → CDI: {len(df)} dias ({df.index[0].date()} a {df.index[-1].date()})")
        return df
    except Exception as e:
        log.warning(f"  ✗ Falha CDI: {e}")
        return pd.Series(dtype=float)


def baixar_ipca(anos: int = 5) -> pd.Series:
    """Retorna série mensal do IPCA (variação decimal mensal)."""
    log.info("Baixando IPCA (BCB/IBGE)...")
    inicio = (date.today() - timedelta(days=anos * 365)).strftime('%d/%m/%Y')
    fim = date.today().strftime('%d/%m/%Y')
    try:
        r = requests.get(BCB_IPCA_URL.format(inicio, fim), timeout=30)
        r.raise_for_status()
        data = r.json()
        df = pd.DataFrame(data)
        df['data'] = pd.to_datetime(df['data'], dayfirst=True)
        df['valor'] = pd.to_numeric(df['valor'], errors='coerce') / 100
        df = df.set_index('data')['valor'].dropna().sort_index()
        log.info(f"  → IPCA: {len(df)} meses")
        return df
    except Exception as e:
        log.warning(f"  ✗ Falha IPCA: {e}")
        return pd.Series(dtype=float)


def construir_ipca_mais_spread(serie_ipca: pd.Series, spread_anual: float, serie_cdi: pd.Series) -> pd.Series:
    """
    Constrói série diária de IPCA + spread.
    Estratégia: converte IPCA mensal → diário via dias úteis no mês, soma spread diário.
    """
    if serie_ipca.empty or serie_cdi.empty:
        return pd.Series(dtype=float)

    # Spread diário equivalente
    spread_diario = (1 + spread_anual) ** (1 / DIAS_UTEIS_ANO) - 1

    # Expande IPCA mensal para diário usando dias úteis do CDI como calendário
    idx_diario = serie_cdi.index
    resultado = pd.Series(index=idx_diario, dtype=float)

    for dt in idx_diario:
        mes_inicio = dt.replace(day=1)
        # Encontra o IPCA do mês
        if mes_inicio in serie_ipca.index:
            ipca_mes = serie_ipca[mes_inicio]
        else:
            # Tenta mês anterior (dados podem atrasar)
            mes_ant = (mes_inicio - timedelta(days=1)).replace(day=1)
            ipca_mes = serie_ipca.get(mes_ant, 0.0)

        # Conta dias úteis no mês para prorrateamento
        dias_uteis_mes = len(idx_diario[(idx_diario.year == dt.year) & (idx_diario.month == dt.month)])
        ipca_diario = (1 + ipca_mes) ** (1 / max(dias_uteis_mes, 1)) - 1

        resultado[dt] = ipca_diario + spread_diario

    return resultado


def _parsear_csv_ihfa(conteudo_bytes: bytes) -> pd.Series:
    """Tenta parsear CSV do IHFA em diferentes formatos que a ANBIMA já usou."""
    for encoding in ['latin-1', 'utf-8']:
        for sep in [';', ',']:
            for header_row in [0, 1, 2]:
                try:
                    df = pd.read_csv(
                        io.StringIO(conteudo_bytes.decode(encoding)),
                        sep=sep, header=header_row, dtype=str,
                    )
                    if df.shape[1] < 2:
                        continue
                    df.columns = [c.strip() for c in df.columns]
                    data_col = next(
                        (c for c in df.columns if any(p in c.lower() for p in ['data','dt','date'])),
                        df.columns[0]
                    )
                    var_col = next(
                        (c for c in df.columns if any(p in c.lower() for p in ['var','ret','%','indice','ihfa'])),
                        df.columns[1]
                    )
                    df[data_col] = pd.to_datetime(df[data_col], dayfirst=True, errors='coerce')
                    df[var_col]  = pd.to_numeric(df[var_col].str.replace(',','.', regex=False), errors='coerce') / 100
                    serie = df.dropna(subset=[data_col, var_col]).set_index(data_col)[var_col].sort_index()
                    if len(serie) > 100:
                        return serie
                except Exception:
                    continue
    return pd.Series(dtype=float)


def baixar_ihfa(caminho_local: str = None) -> pd.Series:
    """
    Tenta obter série diária do IHFA.

    Ordem de tentativa:
      1. Arquivo local (--ihfa caminho/para/ihfa.csv) — mais confiável
      2. Download automático via URLs conhecidas da ANBIMA

    Como baixar manualmente (caso o download automático falhe):
      1. Acesse: https://www.anbima.com.br/pt_br/informar/precos-e-indices/indices/ihfa.htm
      2. Clique em 'Download' ou 'Série Histórica'
      3. Salve como ihfa.csv na pasta AlphaDesk
      4. Rode: python pipeline_fundos.py --xp lista-fundos.xlsx --ihfa ihfa.csv
    """
    if caminho_local and os.path.exists(caminho_local):
        log.info(f"Carregando IHFA do arquivo local: {caminho_local}")
        try:
            with open(caminho_local, 'rb') as f:
                serie = _parsear_csv_ihfa(f.read())
            if not serie.empty:
                log.info(f"  → IHFA: {len(serie)} dias ({serie.index[0].date()} a {serie.index[-1].date()})")
                return serie
            log.warning("  ✗ Não foi possível parsear o arquivo IHFA local")
        except Exception as e:
            log.warning(f"  ✗ Erro ao ler arquivo IHFA local: {e}")

    log.info("Baixando IHFA (ANBIMA — tentando URLs conhecidas)...")
    urls_tentar = [
        "https://www.anbima.com.br/informacoes/fundos/resultado-de-indices/arqs/ihfa_diario.csv",
        "https://www.anbima.com.br/pt_br/informar/precos-e-indices/indices/arqs/ihfa_diario.csv",
        "https://data.anbima.com.br/ihfa/serie-historica/download",
    ]
    for url in urls_tentar:
        try:
            r = requests.get(url, timeout=30, headers={'User-Agent': 'Mozilla/5.0'})
            if r.status_code == 200 and len(r.content) > 1000:
                serie = _parsear_csv_ihfa(r.content)
                if not serie.empty:
                    log.info(f"  → IHFA: {len(serie)} dias via {url}")
                    return serie
        except Exception:
            continue

    log.warning(
        "  ✗ IHFA não disponível automaticamente.\n"
        "     Para incluir: baixe a série histórica em anbima.com.br e rode com --ihfa ihfa.csv\n"
        "     O dashboard funcionará normalmente sem o IHFA."
    )
    return pd.Series(dtype=float)


def _serie_para_dict(serie: pd.Series) -> dict:
    """Converte pd.Series com index datetime para dict {date_str: float}."""
    return {str(k.date()): round(float(v), 8) for k, v in serie.items() if pd.notna(v)}


def calcular_acumulado(serie_diaria: pd.Series) -> pd.Series:
    """Converte série de retornos diários em índice acumulado (base 100)."""
    return (1 + serie_diaria).cumprod() * 100


def montar_benchmarks(serie_cdi: pd.Series, serie_ipca: pd.Series, serie_ihfa: pd.Series, serie_imab: pd.Series = None) -> dict:
    """
    Monta o objeto benchmarks.json com séries históricas e acumulados.
    """
    log.info("Montando benchmarks...")

    benchmarks = {}

    # CDI
    if not serie_cdi.empty:
        acum = calcular_acumulado(serie_cdi)
        benchmarks['CDI'] = {
            'nome': 'CDI',
            'tipo': 'taxa',
            'retornos_diarios': _serie_para_dict(serie_cdi),
            'acumulado': _serie_para_dict(acum),
        }

    # IHFA
    if not serie_ihfa.empty:
        acum = calcular_acumulado(serie_ihfa)
        benchmarks['IHFA'] = {
            'nome': 'IHFA',
            'tipo': 'indice',
            'retornos_diarios': _serie_para_dict(serie_ihfa),
            'acumulado': _serie_para_dict(acum),
        }

    # IMA-B
    if serie_imab is not None and not serie_imab.empty:
        acum = calcular_acumulado(serie_imab)
        benchmarks['IMA-B'] = {
            'nome': 'IMA-B',
            'tipo': 'indice',
            'retornos_diarios': _serie_para_dict(serie_imab),
            'acumulado': _serie_para_dict(acum),
        }

    # IPCA + spreads
    for label, spread in IPCA_SPREADS.items():
        serie = construir_ipca_mais_spread(serie_ipca, spread, serie_cdi)
        if not serie.empty:
            acum = calcular_acumulado(serie)
            benchmarks[label] = {
                'nome': label,
                'tipo': 'ipca_spread',
                'spread_anual': spread,
                'retornos_diarios': _serie_para_dict(serie),
                'acumulado': _serie_para_dict(acum),
            }

    log.info(f"  → {len(benchmarks)} benchmarks montados: {list(benchmarks.keys())}")
    return benchmarks


# ---------------------------------------------------------------------------
# ETAPA 6 — Arquivos manuais (recomendados, gestoras, conteudo)
# ---------------------------------------------------------------------------

TEMPLATE_RECOMENDADOS = {
    "_instrucoes": "Adicione CNPJs dos fundos recomendados/aprovados. Atualize manualmente a cada revisão semestral.",
    "_ultima_revisao": "",
    "recomendados": [],
    "aprovados": []
}

TEMPLATE_GESTORAS = {
    "_instrucoes": "Preencha uma entrada por gestora. Use o NOME_GESTORA exatamente como aparece na planilha XP.",
    "gestoras": [
        {
            "nome": "Nome da Gestora",
            "nome_xp": "Nome exato conforme planilha XP",
            "fundacao": "1999",
            "cidade": "São Paulo",
            "aum_bilhoes": 0.0,
            "filosofia": "Descreva a filosofia e estratégia de investimento.",
            "equipe": [
                {"nome": "Nome do Gestor", "cargo": "CIO / Gestor Principal", "anos_casa": 0}
            ],
            "premiacoes": [],
            "tags": []
        }
    ]
}

TEMPLATE_CONTEUDO = {
    "_instrucoes": "Adicione cartas, podcasts e relatórios manualmente. Tipo: 'carta' | 'podcast' | 'relatorio'",
    "itens": [
        {
            "gestora": "Nome da Gestora",
            "tipo": "carta",
            "titulo": "Carta do Gestor — Mês/Ano",
            "descricao": "Breve descrição do conteúdo",
            "url": "https://...",
            "data": "2025-01-15",
            "duracao_ou_paginas": "8 páginas"
        }
    ]
}


def criar_arquivos_manuais_se_nao_existem(output_dir: Path):
    """Cria templates dos arquivos manuais caso ainda não existam."""
    arquivos = {
        'recomendados.json': TEMPLATE_RECOMENDADOS,
        'gestoras.json': TEMPLATE_GESTORAS,
        'conteudo.json': TEMPLATE_CONTEUDO,
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    for nome, template in arquivos.items():
        caminho = output_dir / nome
        if not caminho.exists():
            with open(caminho, 'w', encoding='utf-8') as f:
                json.dump(template, f, ensure_ascii=False, indent=2)
            log.info(f"  → Template criado: {nome} (preencha manualmente)")
        else:
            log.info(f"  → Arquivo manual já existe: {nome} (mantido)")


def aplicar_recomendados(df_fundos: pd.DataFrame, output_dir: Path) -> pd.DataFrame:
    """Marca fundos como recomendados/aprovados conforme arquivo manual."""
    caminho = output_dir / 'recomendados.json'
    if not caminho.exists():
        return df_fundos
    try:
        with open(caminho, encoding='utf-8') as f:
            data = json.load(f)
        import re as _re
        _norm = lambda c: _re.sub(r'[./-]', '', str(c))
        cnpjs_rec  = set(_norm(c) for c in data.get('recomendados', []))
        cnpjs_apr  = set(_norm(c) for c in data.get('aprovados', []))
        df_fundos['recomendado'] = df_fundos['cnpj'].apply(_norm).isin(cnpjs_rec)
        df_fundos['aprovado']    = df_fundos['cnpj'].apply(_norm).isin(cnpjs_apr)
        log.info(f"  → {len(cnpjs_rec)} recomendados | {len(cnpjs_apr)} aprovados aplicados")
    except Exception as e:
        log.warning(f"  ✗ Falha ao ler recomendados.json: {e}")
    return df_fundos


# ---------------------------------------------------------------------------
# ETAPA 7 — Geração dos outputs
# [SHARED-RF] _to_serializable — importada pelo scripts/pipeline_fundos_rf.py.
#             Alterar assinatura quebra o RF.
# ---------------------------------------------------------------------------

def _to_serializable(val):
    """Garante que tipos numpy/pandas sejam serializáveis em JSON."""
    if isinstance(val, (np.integer,)):   return int(val)
    if isinstance(val, (np.floating,)):  return float(val) if not np.isnan(val) else None
    if isinstance(val, (np.bool_,)):     return bool(val)
    if pd.isna(val):                     return None
    return val


def gerar_fundos_json(df_fundos: pd.DataFrame) -> list[dict]:
    """Converte DataFrame em lista de dicts limpos para JSON."""
    colunas_excluir = {'_ret', '_cotas'}
    registros = []
    for _, row in df_fundos.iterrows():
        d = {}
        for k, v in row.items():
            if k in colunas_excluir:
                continue
            d[k] = _to_serializable(v)
        registros.append(d)
    return registros


def salvar_outputs(
    df_fundos: pd.DataFrame,
    benchmarks: dict,
    output_dir: Path,
):
    output_dir.mkdir(parents=True, exist_ok=True)

    # fundos.json
    fundos_list = gerar_fundos_json(df_fundos)
    # Filtro: remove fundos sem dados confiáveis de volatilidade (sem série CVM)
    antes = len(fundos_list)
    fundos_list = [f for f in fundos_list if f.get('volatilidade') and f['volatilidade'] != 0]
    if antes != len(fundos_list):
        log.info(f"  Filtro volatilidade: {antes} → {len(fundos_list)} fundos ({antes - len(fundos_list)} sem dados CVM removidos)")
    with open(output_dir / 'fundos.json', 'w', encoding='utf-8') as f:
        json.dump(fundos_list, f, ensure_ascii=False, indent=2, cls=NumpyEncoder)
    log.info(f"  ✓ fundos.json → {len(fundos_list)} fundos")

    # benchmarks.json
    with open(output_dir / 'benchmarks.json', 'w', encoding='utf-8') as f:
        json.dump(benchmarks, f, ensure_ascii=False)
    log.info(f"  ✓ benchmarks.json → {len(benchmarks)} séries")
    log.info(f"  ✓ benchmarks.json → {len(benchmarks)} séries")

    # cotas.json — séries diárias normalizadas (base 100) para gráficos
    # Lê do cache CVM para montar as séries
    cotas_dict = {}
    cache_dir = output_dir / 'cache_cvm'
    if cache_dir.exists():
        import glob
        parquet_files = sorted(glob.glob(str(cache_dir / '*.parquet')))
        if parquet_files:
            try:
                import pyarrow.parquet as pq
                frames_c = []
                for pf in parquet_files:
                    frames_c.append(pd.read_parquet(pf))
                df_c = pd.concat(frames_c, ignore_index=True) if frames_c else pd.DataFrame()
                if not df_c.empty:
                    df_c['DT_COMPTC'] = pd.to_datetime(df_c['DT_COMPTC'])
                    log.info(f"  cotas — colunas disponíveis: {list(df_c.columns)}")
                    # Detect VL_QUOTA column — CVM may rename it
                    quota_col = next(
                        (col for col in df_c.columns
                         if 'QUOTA' in col.upper() or 'VL_COTA' in col.upper()),
                        None
                    )
                    if not quota_col:
                        log.warning(f"  ✗ cotas.json: coluna VL_QUOTA não encontrada. Colunas: {list(df_c.columns)}")
                    else:
                        if quota_col != 'VL_QUOTA':
                            log.info(f"  cotas — renomeando {quota_col} → VL_QUOTA")
                            df_c = df_c.rename(columns={quota_col: 'VL_QUOTA'})
                        # Ensure numeric (old CVM format uses comma as decimal)
                        if df_c['VL_QUOTA'].dtype == object:
                            df_c['VL_QUOTA'] = pd.to_numeric(
                                df_c['VL_QUOTA'].str.replace(',', '.', regex=False), errors='coerce')
                        else:
                            df_c['VL_QUOTA'] = pd.to_numeric(df_c['VL_QUOTA'], errors='coerce')
                        df_c = df_c.sort_values(['CNPJ_NORM', 'DT_COMPTC'])
                        cnpj_map = {re.sub(r'[./-]', '', f['cnpj']): f['cnpj'] for f in fundos_list}
                        matched = 0
                        for cnpj_norm, grp in df_c.groupby('CNPJ_NORM'):
                            cnpj_orig = cnpj_map.get(cnpj_norm)
                            if not cnpj_orig: continue
                            grp = grp.sort_values('DT_COMPTC')
                            # Dedup por continuidade: mantém cota mais próxima do dia anterior
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
                            if len(cotas) < 2: continue
                            base = cotas.iloc[0]
                            norm = (cotas / base * 100).round(4)
                            cotas_dict[cnpj_orig] = {
                                'datas':   [d.strftime('%Y-%m-%d') for d in norm.index],
                                'valores': norm.tolist(),
                            }
                            matched += 1
                        log.info(f"  cotas — {matched} fundos com série, {len(cnpj_map)} fundos na planilha")
                log.info(f"  ✓ cotas.json → {len(cotas_dict)} séries")
            except Exception as e:
                log.warning(f"  ✗ cotas.json não gerado: {e}")
    # Only write cotas.json if we generated series — never overwrite a good file with {}
    cotas_path = output_dir / 'cotas.json'
    if cotas_dict:
        with open(cotas_path, 'w', encoding='utf-8') as f:
            json.dump(cotas_dict, f, ensure_ascii=False, cls=NumpyEncoder)
        log.info(f"  ✓ cotas.json → {len(cotas_dict)} séries ({cotas_path.stat().st_size/1024/1024:.1f} MB)")
    else:
        existing_size = cotas_path.stat().st_size if cotas_path.exists() else 0
        if existing_size > 1000:
            log.info(f"  ✓ cotas.json — mantido arquivo existente ({existing_size/1024/1024:.1f} MB, {{}}-overwrite bloqueado)")
        else:
            with open(cotas_path, 'w', encoding='utf-8') as f:
                json.dump({}, f)
            log.warning("  ⚠ cotas.json → vazio (cache CVM não encontrado — rode gerar_cotas.py)")

    # meta.json
    meta = {
        "ultima_atualizacao": datetime.now().isoformat(),
        "total_fundos": len(fundos_list),
        "fundos_captacao_aberta": sum(1 for f in fundos_list if f.get('captacao_aberta')),
        "recomendados": sum(1 for f in fundos_list if f.get('recomendado')),
        "aprovados": sum(1 for f in fundos_list if f.get('aprovado')),
        "benchmarks_disponiveis": list(benchmarks.keys()),
        "classes_xp": sorted(set(f.get('class_xp', '') for f in fundos_list)),
    }
    with open(output_dir / 'meta.json', 'w', encoding='utf-8') as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)
    log.info(f"  ✓ meta.json")

    log.info(f"\n✅ Todos os arquivos salvos em: {output_dir.resolve()}")


# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description='Alpha Desk — Pipeline de dados')
    parser.add_argument('--xp',     required=True, help='Caminho da planilha XP (.xlsx)')
    parser.add_argument('--output', default='./data', help='Pasta de saída (padrão: ./data)')
    parser.add_argument('--sem-cvm', action='store_true',
                        help='Pula download da CVM (mais rápido, sem métricas quant)')
    parser.add_argument('--anos-historico', type=int, default=5,
                        help='Anos de histórico de cotas a baixar da CVM (padrão: 5 anos)')
    parser.add_argument('--ihfa', default=None,
                        help='Caminho para CSV do IHFA (ex: ihfa.csv)')
    parser.add_argument('--imab', default=None,
                        help='Caminho para CSV do IMA-B (ex: imab.csv)')
    args = parser.parse_args()

    output_dir = Path(args.output)
    global MESES_HISTORICO
    MESES_HISTORICO = args.anos_historico * 12

    log.info("=" * 60)
    log.info("ALPHA DESK — Pipeline de Dados")
    log.info("=" * 60)

    # --- 1. Planilha XP
    df = ler_planilha_xp(args.xp)

    # --- 2. Arquivos manuais (cria templates se não existir)
    log.info("\n[Arquivos manuais]")
    criar_arquivos_manuais_se_nao_existem(output_dir)
    df = aplicar_recomendados(df, output_dir)

    if not args.sem_cvm:
        # --- 3. CVM: cadastro (data de início)
        log.info("\n[CVM — Cadastro]")
        df_cad = baixar_cadastro_cvm()
        df = enriquecer_cadastro(df, df_cad)

        # --- 4. CVM: informes diários
        log.info("\n[CVM — Informes Diários]")
        cnpjs = set(df['cnpj'].tolist())
        df_cotas = baixar_informes_cvm(cnpjs, n_meses=MESES_HISTORICO, pasta_cache=output_dir / "cache_cvm")
        df, metricas_temp = enriquecer_metricas(df, df_cotas)
    else:
        log.info("\n[CVM] Pulando download (--sem-cvm ativo)")

    # --- 5. Benchmarks
    log.info("\n[Benchmarks]")
    serie_cdi  = baixar_cdi()
    serie_ipca = baixar_ipca()
    serie_ihfa = baixar_ihfa(caminho_local=args.ihfa)

    # IMA-B — carrega do CSV (--imab) ou do benchmarks.json existente
    serie_imab = pd.Series(dtype=float)
    if args.imab and os.path.exists(args.imab):
        log.info(f'  Carregando IMA-B do CSV: {args.imab}')
        try:
            with open(args.imab, 'rb') as f:
                serie_imab = _parsear_csv_ihfa(f.read())
            log.info(f'  IMA-B: {len(serie_imab)} dias carregados do CSV')
        except Exception as e:
            log.warning(f'  Erro ao ler CSV do IMA-B: {e}')
    else:
        bench_existente_path = output_dir / 'benchmarks.json'
        if bench_existente_path.exists():
            try:
                bench_existente = json.loads(bench_existente_path.read_text(encoding='utf-8'))
                if 'IMA-B' in bench_existente and bench_existente['IMA-B'].get('retornos_diarios'):
                    rd = bench_existente['IMA-B']['retornos_diarios']
                    serie_imab = pd.Series(
                        {pd.Timestamp(k): v for k, v in rd.items()},
                        dtype=float
                    ).sort_index()
                    log.info(f'  IMA-B carregado do benchmarks.json: {len(serie_imab)} dias')
            except Exception as e:
                log.warning(f'  Não foi possível carregar IMA-B do benchmarks.json: {e}')
    if serie_imab.empty:
        log.info('  IMA-B não encontrado — gráficos ficarão sem esse índice.')

    # Segunda passagem: Sharpe e Sortino (precisa do CDI)
    if not args.sem_cvm and not serie_cdi.empty:
        log.info("\n[Sharpe / Sortino — segunda passagem]")
        df = segunda_passagem_sharpe(df, serie_cdi, metricas_temp)

    benchmarks = montar_benchmarks(serie_cdi, serie_ipca, serie_ihfa, serie_imab)

    # --- 6. Salvar
    log.info("\n[Salvando outputs]")
    salvar_outputs(df, benchmarks, output_dir)

    # --- Resumo final
    log.info("\n" + "=" * 60)
    log.info("RESUMO")
    log.info("=" * 60)
    log.info(f"  Fundos processados:    {len(df)}")
    log.info(f"  Com PL calculado:      {df['pl'].notna().sum()}")
    log.info(f"  Com Sharpe calculado:  {df['sharpe'].notna().sum()}")
    log.info(f"  Com Drawdown calc.:    {df['drawdown_max'].notna().sum()}")
    log.info(f"  Recomendados:          {df['recomendado'].sum()}")
    log.info(f"  Benchmarks:            {list(benchmarks.keys())}")
    log.info("=" * 60)


if __name__ == '__main__':
    main()
