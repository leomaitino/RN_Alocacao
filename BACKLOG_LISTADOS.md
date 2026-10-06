# Backlog — Dashboard de Fundos Listados (FI-Infra, FII de papel, Fiagro)

Produto criado em 06/10/2026 a partir da planilha `Painel Fundos Listados`
(22 fundos; 6 FIP-IE excluídos por serem equity de infraestrutura). Arquivos:

- `scripts/pipeline_listados.py` — coleta e cálculo (Yahoo, CVM diário/mensal,
  CDA look-through, SND, eventos) → `data/listados*.json`, `data/meta_listados.json`
- `scripts/exportar_painel_listados.py` — planilha preenchida em `output/`
- `data/listados_universo.json` — universo curado (CNPJ, gestor, status, teses)
- `data/listados_eventos_stress.json` — eventos de stress curados
- `dashboard_listados.html` — 5 abas (Painel · Preço × VP · Carteira & Stress · Sugestões · Fontes)

Limitações conhecidas e próximos passos, em ordem de valor:

---

## 1. Carteira dos FI-Infra com posições confidenciais na CDA

**Estado:** o look-through FIC → master funciona onde a CDA é aberta
(KDIF11, BINC11, CPTI11, BDIF11, JURO11, RBIF11, IFRI11/IFRA11 parcial, XPID11).
Sparta (CDII11, DIVS11), Inter (BIDB11), Suno (SNID11), Bocaina (BODB11),
Iridium (IRIF11), Vinland (VINF11) e Órama (OGIN11) mantêm as debêntures em
posição confidencial (`confidencial_pct` ≈ 100%); só títulos públicos e caixa
aparecem.

**Próximo passo:** para esses fundos, carregar a carteira do relatório
gerencial (PDF/FNET) num JSON manual `data/listados_carteiras_manual.json`
com o mesmo esquema de `ativos` (código ou emissor, % PL, indexador, taxa),
que o pipeline usaria como fallback quando `confidencial_pct > 0.5`. O FNET
bloqueou acesso programático (curl e browser retornaram vazio em out/2026);
os PDFs estão nos sites dos gestores.

## 2. Carteira por ativo dos FII e Fiagro (CRI/CRA)

**Estado:** a CVM só publica a composição agregada (CRI, CRA, caixa, FII…)
no informe mensal. O dashboard mostra essa composição e os eventos de stress
são curados por fundo (`escopo: fundo`).

**Próximo passo:** mesmo JSON manual do item 1, alimentado pelos relatórios
gerenciais (Kinea, Valora, Capitânia, Itaú publicam tabelas com devedor,
indexador, taxa e LTV). Permitiria stress por devedor de CRI/CRA, hoje
inexistente.

## 3. Proventos do Yahoo: lacunas e casas decimais

**Estado:** `corrigir_proventos_yahoo` divide por 10 valores > 6× a mediana
vizinha quando v/10 cai na faixa (casos reais: AAZQ11 jun/26, VCRI11 nov/25,
BINC11 jun/25). Meses faltantes (ex.: KNCA11 abr/26) subestimam o DY 12M;
a coluna `n_proventos_12m` deixa a lacuna visível.

**Próximo passo:** usar o `Percentual_Dividend_Yield_Mes` do informe mensal
CVM (FII/Fiagro) para reconstruir o provento mensal (`DY_mes × VP`) e
preencher meses ausentes; para FI-Infra, cruzar com a variação de cota no
dia ex (queda ≈ provento).

## 4. Peso no IFIX e composição de índices

**Estado:** coluna vazia. A API da B3 (`sistemaswebb3-listados`) respondia
500/vazio em out/2026, tanto por curl quanto pelo browser.

**Próximo passo:** tentar `https://sistemaswebb3-listados.b3.com.br/indexPage/day/IFIX`
quando voltar, ou ler a carteira teórica do IFIX publicada em CSV pela B3.

## 5. TIR / taxa média da carteira

**Estado:** `tir_relatorio` é manual (copiado do painel original). O
`cupom_medio` calculado do SND é taxa de EMISSÃO ponderada por exposição,
não marcação a mercado — útil como proxy do carrego, não como TIR.

**Próximo passo:** com o item 1/2 resolvido, usar a taxa média MTM do
relatório gerencial. Alternativa automática: preços indicativos ANBIMA de
debêntures (data.anbima.com.br) para marcar cada debênture da CDA.

## 6. AZIN11 (FIP-IE de dívida) sem informe diário

**Estado:** VP manual (`vp_manual` no universo, 97,90 em 31/08/2026). FIPs
publicam informe trimestral na CVM (dataset `FIP/DOC/INF_TRIMESTRAL`).

**Próximo passo:** ler o informe trimestral de FIP para VP e PL; ou aceitar
a atualização manual mensal pelo relatório gerencial.

## 7. Status Aprovado/Não aprovado editável no dashboard

**Estado:** somente leitura; status vive em `data/listados_universo.json`
e muda por commit. Replicar o fluxo de senha + `POST /api/save-*` dos
dashboards MM/RF só faz sentido com o modo read-only de produção já
existente (Render perde disco na hibernação).

## 8. Recomendações: regra de corte formalizada

**Estado:** as 10 sugestões usaram corte qualitativo (PL ≥ R$ 500 mi,
liquidez ≥ R$ 1 mi/dia, gestor institucional, sem evento aberto). Está no
texto da aba Sugestões e nas teses do universo.

**Próximo passo:** transformar em score (como nos dashboards MM/RF) com
P/VP vs. média 12M, DY, liquidez, tamanho, stress e consistência de
proventos — pede ≥ 12 meses de histórico de P/VP, que o pipeline já guarda.

## 9. Ratings das debêntures

**Estado:** a CDA (BLC_4) não traz rating das debêntures; só BLC_5/BLC_7
(títulos de IF e exterior) têm `GRAU_RISCO`. O SND também não.

**Próximo passo:** ratings públicos por emissor (Fitch/Moody's/S&P nacionais)
num JSON manual, ou scraping dos relatórios de rating linkados nos eventos.

## 10. Cache do informe diário por universo

**Estado:** `data/cache_cvm_listados/inf_diario_<hash>/` — o hash é do
conjunto de CNPJs FI-Infra. Incluir/retirar um FI-Infra do universo gera um
cache novo (re-download de 36 meses, ~2 min). Pastas antigas podem ser
apagadas manualmente.
