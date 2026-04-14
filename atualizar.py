#!/usr/bin/env python3
"""
╔══════════════════════════════════════════════════════════════╗
║           ALPHA DESK — Atualização Unificada                ║
╚══════════════════════════════════════════════════════════════╝

Um comando para atualizar tudo:

    python atualizar.py

O que faz (em ordem):
  1. Detecta a planilha XP mais recente em input/
  2. Roda o pipeline de fundos (CVM + métricas + benchmarks)
  3. Atualiza dados de mercado (IBOV, Dólar, S&P, etc.)
  4. Atualiza o comparador de carteiras
  5. Gera resumo das mudanças

Opções:
    python atualizar.py --rapido          # pula CVM (só atualiza XP + benchmarks)
    python atualizar.py --xp planilha.xlsx  # usa planilha específica
    python atualizar.py --sem-mercado     # pula dados de mercado (yfinance)
    python atualizar.py --sem-comparador  # pula atualização do comparador
"""

import argparse
import json
import os
import sys
import subprocess
import logging
import time
from pathlib import Path
from datetime import datetime

# ── Config ────────────────────────────────────────────────────────────────────
BASE_DIR = Path(os.path.dirname(os.path.abspath(__file__)))
INPUT_DIR = BASE_DIR / "input"
DATA_DIR = BASE_DIR / "data"
SCRIPTS_DIR = BASE_DIR / "scripts"
COMPARADOR_DIR = BASE_DIR / "comparador"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(message)s",
    datefmt="%H:%M:%S"
)
log = logging.getLogger("atualizar")


# ── Helpers ───────────────────────────────────────────────────────────────────

def encontrar_planilha_xp() -> Path | None:
    """Encontra a planilha XP mais recente na pasta input/."""
    if not INPUT_DIR.exists():
        return None
    candidatas = list(INPUT_DIR.glob("lista-fundos*.xlsx"))
    if not candidatas:
        return None
    # Ordena por data de modificação (mais recente primeiro)
    candidatas.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return candidatas[0]


def carregar_estado_anterior() -> dict:
    """Carrega fundos.json e meta.json antes da atualização para comparação."""
    estado = {"fundos": [], "meta": {}}
    fundos_path = DATA_DIR / "fundos.json"
    meta_path = DATA_DIR / "meta.json"
    if fundos_path.exists():
        try:
            with open(fundos_path, encoding="utf-8") as f:
                estado["fundos"] = json.load(f)
        except Exception:
            pass
    if meta_path.exists():
        try:
            with open(meta_path, encoding="utf-8") as f:
                estado["meta"] = json.load(f)
        except Exception:
            pass
    return estado


def rodar_comando(descricao: str, cmd: list[str], cwd: Path = None) -> bool:
    """Executa um comando e mostra progresso."""
    log.info(f"")
    log.info(f"{'─' * 60}")
    log.info(f"  {descricao}")
    log.info(f"{'─' * 60}")
    inicio = time.time()
    try:
        result = subprocess.run(
            cmd,
            cwd=str(cwd or BASE_DIR),
            capture_output=False,
            text=True,
            timeout=1800,  # 30 min max
        )
        duracao = time.time() - inicio
        if result.returncode == 0:
            log.info(f"  ✓ Concluído em {duracao:.0f}s")
            return True
        else:
            log.error(f"  ✗ Falhou (código {result.returncode}) em {duracao:.0f}s")
            return False
    except subprocess.TimeoutExpired:
        log.error(f"  ✗ Timeout (>30 min)")
        return False
    except Exception as e:
        log.error(f"  ✗ Erro: {e}")
        return False


def gerar_resumo(estado_antes: dict):
    """Compara estado anterior com o novo e gera resumo das mudanças."""
    log.info(f"")
    log.info(f"{'═' * 60}")
    log.info(f"  RESUMO DA ATUALIZAÇÃO")
    log.info(f"{'═' * 60}")

    # Carrega estado novo
    fundos_novos = []
    meta_nova = {}
    try:
        with open(DATA_DIR / "fundos.json", encoding="utf-8") as f:
            fundos_novos = json.load(f)
    except Exception:
        log.warning("  Não foi possível ler fundos.json atualizado")
        return
    try:
        with open(DATA_DIR / "meta.json", encoding="utf-8") as f:
            meta_nova = json.load(f)
    except Exception:
        pass

    fundos_antes = estado_antes.get("fundos", [])
    meta_antes = estado_antes.get("meta", {})

    # CNPJs antes e depois
    cnpjs_antes = {f["cnpj"] for f in fundos_antes}
    cnpjs_depois = {f["cnpj"] for f in fundos_novos}
    cnpjs_novos = cnpjs_depois - cnpjs_antes
    cnpjs_removidos = cnpjs_antes - cnpjs_depois

    # Resumo geral
    log.info(f"")
    log.info(f"  Total de fundos:    {len(fundos_novos)}")
    log.info(f"  Captação aberta:    {sum(1 for f in fundos_novos if f.get('captacao_aberta'))}")
    log.info(f"  Recomendados:       {sum(1 for f in fundos_novos if f.get('recomendado'))}")
    log.info(f"  Benchmarks:         {', '.join(meta_nova.get('benchmarks_disponiveis', []))}")
    log.info(f"  Última atualização: {meta_nova.get('ultima_atualizacao', 'N/A')[:19]}")

    # Fundos novos
    if cnpjs_novos:
        log.info(f"")
        log.info(f"  ┌── FUNDOS NOVOS ({len(cnpjs_novos)}):")
        for f in fundos_novos:
            if f["cnpj"] in cnpjs_novos:
                log.info(f"  │  + {f['nome'][:50]} ({f['gestora']})")
        log.info(f"  └──")

    # Fundos removidos
    if cnpjs_removidos:
        log.info(f"")
        log.info(f"  ┌── FUNDOS REMOVIDOS ({len(cnpjs_removidos)}):")
        for f in fundos_antes:
            if f["cnpj"] in cnpjs_removidos:
                log.info(f"  │  - {f['nome'][:50]} ({f['gestora']})")
        log.info(f"  └──")

    # Top 10 por Sharpe
    recomendados = [f for f in fundos_novos if f.get("recomendado")]
    if recomendados:
        log.info(f"")
        log.info(f"  ┌── RECOMENDADOS — Performance do Mês:")
        for f in sorted(recomendados, key=lambda x: x.get("rent_mes") or 0, reverse=True):
            rent = f.get("rent_mes")
            rent_str = f"{rent*100:+.2f}%" if rent else "N/A"
            sharpe_str = f"{f.get('sharpe', 0):.2f}" if f.get('sharpe') else "N/A"
            log.info(f"  │  {f['nome'][:40]:<42} Mês: {rent_str:>8}  Sharpe: {sharpe_str}")
        log.info(f"  └──")

    # Alertas: fundos com drawdown alto ou vol subindo
    if fundos_antes:
        mapa_antes = {f["cnpj"]: f for f in fundos_antes}
        alertas = []
        for f in fundos_novos:
            fa = mapa_antes.get(f["cnpj"])
            if not fa:
                continue
            # Drawdown aumentou mais de 2pp
            dd_novo = f.get("drawdown_max") or 0
            dd_antes = fa.get("drawdown_max") or 0
            if dd_novo > dd_antes + 0.02 and f.get("recomendado"):
                alertas.append(f"  │  ⚠ {f['nome'][:40]} — Drawdown: {dd_antes*100:.1f}% → {dd_novo*100:.1f}%")
            # Volatilidade subiu mais de 50%
            vol_novo = f.get("volatilidade") or 0
            vol_antes = fa.get("volatilidade") or 0
            if vol_antes > 0 and vol_novo > vol_antes * 1.5 and f.get("recomendado"):
                alertas.append(f"  │  ⚠ {f['nome'][:40]} — Vol: {vol_antes*100:.1f}% → {vol_novo*100:.1f}%")
            # PL caiu mais de 20% em 12m
            var_pl = f.get("variacao_pl_12m")
            if var_pl is not None and var_pl < -0.20 and f.get("recomendado"):
                alertas.append(f"  │  ⚠ {f['nome'][:40]} — PL 12M: {var_pl*100:+.1f}% (resgates)")

        if alertas:
            log.info(f"")
            log.info(f"  ┌── ALERTAS ({len(alertas)}):")
            for a in alertas:
                log.info(a)
            log.info(f"  └──")

    if not cnpjs_novos and not cnpjs_removidos:
        log.info(f"")
        log.info(f"  → Mesma base de fundos, métricas atualizadas.")

    log.info(f"")
    log.info(f"{'═' * 60}")
    log.info(f"  ✅ Atualização concluída!")
    log.info(f"  Para visualizar: python servidor.py → http://localhost:8000")
    log.info(f"{'═' * 60}")


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Alpha Desk — Atualização unificada",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Exemplos:
  python atualizar.py                    # atualização completa
  python atualizar.py --rapido           # sem CVM (mais rápido)
  python atualizar.py --xp minha.xlsx    # planilha específica
        """,
    )
    parser.add_argument("--xp", default=None,
                        help="Planilha XP específica (padrão: detecta em input/)")
    parser.add_argument("--rapido", action="store_true",
                        help="Pula download CVM (usa cache existente, só atualiza XP + benchmarks)")
    parser.add_argument("--sem-mercado", action="store_true",
                        help="Pula atualização de dados de mercado (yfinance)")
    parser.add_argument("--sem-comparador", action="store_true",
                        help="Pula atualização do comparador de carteiras")
    parser.add_argument("--ihfa", default=None,
                        help="CSV do IHFA (padrão: detecta em input/)")
    parser.add_argument("--imab", default=None,
                        help="CSV do IMA-B (padrão: detecta em input/)")
    args = parser.parse_args()

    inicio_total = time.time()

    log.info("╔══════════════════════════════════════════════════════════╗")
    log.info("║           ALPHA DESK — Atualização Unificada           ║")
    log.info("╚══════════════════════════════════════════════════════════╝")
    log.info(f"  Data: {datetime.now().strftime('%d/%m/%Y %H:%M')}")
    log.info(f"  Modo: {'RÁPIDO (sem CVM)' if args.rapido else 'COMPLETO'}")

    # ── 1. Detectar planilha XP ──
    if args.xp:
        planilha = Path(args.xp)
        if not planilha.exists():
            planilha = INPUT_DIR / args.xp
    else:
        planilha = encontrar_planilha_xp()

    if not planilha or not planilha.exists():
        log.error("✗ Nenhuma planilha XP encontrada!")
        log.error("  Coloque 'lista-fundos.xlsx' na pasta input/ ou use --xp")
        sys.exit(1)

    log.info(f"  Planilha: {planilha.name} ({planilha.stat().st_size / 1024:.0f} KB)")
    log.info(f"  Modificada: {datetime.fromtimestamp(planilha.stat().st_mtime).strftime('%d/%m/%Y %H:%M')}")

    # Detectar IHFA e IMA-B se não especificados
    ihfa_path = args.ihfa
    if not ihfa_path:
        ihfa_csv = INPUT_DIR / "ihfa.csv"
        if ihfa_csv.exists():
            ihfa_path = str(ihfa_csv)
            log.info(f"  IHFA:     {ihfa_csv.name} (detectado)")

    imab_path = args.imab
    if not imab_path:
        imab_csv = INPUT_DIR / "imab.csv"
        if imab_csv.exists():
            imab_path = str(imab_csv)
            log.info(f"  IMA-B:    {imab_csv.name} (detectado)")

    # ── 2. Salvar estado anterior para comparação ──
    estado_antes = carregar_estado_anterior()

    # ── 3. Pipeline principal ──
    cmd_pipeline = [
        sys.executable, str(SCRIPTS_DIR / "pipeline_fundos.py"),
        "--xp", str(planilha),
        "--output", str(DATA_DIR),
    ]
    if args.rapido:
        cmd_pipeline.append("--sem-cvm")
    if ihfa_path:
        cmd_pipeline.extend(["--ihfa", ihfa_path])
    if imab_path:
        cmd_pipeline.extend(["--imab", imab_path])

    ok_pipeline = rodar_comando(
        "ETAPA 1/3 — Pipeline de fundos (CVM + métricas + benchmarks)",
        cmd_pipeline,
    )
    if not ok_pipeline:
        log.error("Pipeline falhou. Abortando.")
        sys.exit(1)

    # ── 4. Dados de mercado ──
    if not args.sem_mercado:
        rodar_comando(
            "ETAPA 2/3 — Dados de mercado (IBOV, Dólar, S&P, etc.)",
            [sys.executable, str(SCRIPTS_DIR / "baixar_mercado.py")],
        )
    else:
        log.info("\n  [Mercado] Pulando (--sem-mercado)")

    # ── 5. Comparador ──
    if not args.sem_comparador and (COMPARADOR_DIR / "atualizar_comparador.py").exists():
        rodar_comando(
            "ETAPA 3/3 — Comparador de carteiras",
            [sys.executable, str(COMPARADOR_DIR / "atualizar_comparador.py")],
        )
    else:
        log.info("\n  [Comparador] Pulando")

    # ── 6. Resumo ──
    duracao_total = time.time() - inicio_total
    log.info(f"\n  Tempo total: {duracao_total:.0f}s ({duracao_total/60:.1f} min)")

    gerar_resumo(estado_antes)


if __name__ == "__main__":
    main()
