# Backlog RF — itens a revisitar

Itens de dívida técnica do dashboard de Renda Fixa, registrados durante
a Fase 1 (branch `rf-dashboard`). Cada item descreve o estado atual,
o porquê de adiar, e quando faz sentido reabrir.

Formato de cada entrada: **Decisão atual** · **Problema** · **Por que adiar**
· **Quando revisitar** · **Onde mexer**.

---

## 1. Sharpe `or 0` no pipeline (paridade com MM)

**Decisão atual:** `pipeline_fundos_rf.py` mantém o padrão
`round(calcular_sharpe(...) or 0, 4)`, herdando o comportamento de
`pipeline_fundos.py` em que `None` (sem dados suficientes) vira `0.0`.

**Problema:** confunde "sem dados" com "Sharpe genuinamente zero". O frontend
acaba mostrando `0.000` em vez de `—` para fundos cujo retorno alinhado com o
benchmark não tem dias úteis suficientes (`MIN_DIAS_CALCULO`).

**Por que adiar:** mexer só no RF cria divergência com o MM (mesmo
`calcular_sharpe`, comportamento diferente nas duas pipelines). O fix correto
é unificar: eliminar o `or 0` e tratar `None` no frontend de ambos os
dashboards. É uma mudança coordenada que afeta o ranking quantitativo do MM
em produção, então não cabe na Fase 1 do RF.

**Quando revisitar:** quando consolidarmos o tratamento de "métrica sem
dados" nos dois pipelines (etapa de unificação após o RF estar em produção).

**Onde mexer:**
- `scripts/pipeline_fundos.py` — `segunda_passagem_sharpe`
- `scripts/pipeline_fundos_rf.py` — `segunda_passagem_sharpe_excesso_rf`
- `dashboard.html` e `dashboard_rf.html` — `formatQuantVal` precisa
  diferenciar `0` (zero real) de `null` (sem dados)
- Sortino tem o mesmo padrão — propagar a correção

---

## 2. FIDCs marcam curva — score quantitativo subestima risco real

**Decisão atual:** `pipeline_fundos_rf.py` calcula as mesmas métricas para
FIDCs e Crédito High Grade (vol, drawdown, Sharpe, etc), e o score do
dashboard ranqueia cada subgrupo isoladamente.

**Problema:** FIDCs operam com **marcação na curva**, então drawdown e
volatilidade reportados são artificialmente baixos comparados a fundos de
Crédito High Grade que **marcam a mercado**. Isso faz com que o Score RN de
FIDCs seja sistematicamente mais alto que o de Crédito, mesmo quando o risco
fundamental é equivalente ou maior.

**Por que adiar:** o ranking dentro do subgrupo FIDCs continua válido (FIDC
vs FIDC, todos marcam curva — comparação simétrica). O viés só aparece se
alguém comparar score absoluto entre subgrupos, o que a Etapa 1.3 não vai
incentivar (filtro principal é por subgrupo). Logo, não bloqueia a Fase 1.

**Quando revisitar:** ao consolidar a aba Quantitativa do dashboard RF,
considerar:
  (a) Badge visual na aba FIDCs avisando "marcação na curva — vol/DD
      subestimam risco real comparado a fundos a mercado".
  (b) Métrica complementar baseada em PDD/inadimplência da carteira do
      FIDC, se o dado existir nas lâminas/relatórios da gestora ou em
      alguma base ANBIMA/Uqbar acessível.

**Onde mexer:**
- `dashboard_rf.html` — aba quantitativa, badge de aviso quando subgrupo=FIDCs
- Eventualmente: `scripts/pipeline_fundos_rf.py` para enriquecer fundos
  FIDC com PDD/inadimplência se a fonte for automatizável

---

## 3. FIDCs novos sem informe diário CVM (`sem_dados_cvm: true`)

**Decisão atual:** FIDCs com prefixo CNPJ recente (registros 2024-2025)
ainda não publicam informe diário CVM. O pipeline marca esses fundos com
`sem_dados_cvm: true` no fundos_rf.json e as métricas calculadas (Sharpe,
vol, DD, Sortino, consistência, excesso, var_95, calmar) ficam `None`.
Os fundos aparecem na lista usando rentabilidades e taxas da planilha XP
e são selecionáveis como recomendados (FIDC tem `tem_ranking: true`).

**Lista atual de afetados** (11 fundos — ver auditoria do commit `56a0628`):
- 2 com `class_xp = "Crédito Estruturado"`: ambos Jivemaua Bossanova
- 9 com `class_xp = "Crédito High Yield"` chegando via `class_cvm = FIDC`:
  Verde AM Ipê, Jive BossaNova 90, Solis Antares Pioneiro, Tivio Alt 90/180,
  Brave 180 Advisory, Itaú Crédito Estruturado Alpes III, Valora Vanguard,
  XP Crédito Estruturado 90

**Por que adiar a remediação automática:** quando a CVM começar a publicar
(estimativa: 6-12 meses por fundo), a próxima rodada do pipeline preenche
métricas automaticamente — sem código novo. Para a Fase 1, a flag basta.

**Próximo passo opcional:** se a lista crescer e ficar persistente, avaliar
fontes alternativas (Uqbar, gestora direto, ANBIMA Data) para puxar série
de cota e calcular métricas sob demanda. Não é prioritário.

**Onde mexer:**
- `scripts/pipeline_fundos_rf.py` — função `salvar_outputs_rf` aplica a flag
  e loga `[FIDC_SEM_CVM]` na rodada
- `dashboard_rf.html` — front precisa respeitar a flag: ocultar score e
  exibir badge "sem dados CVM" para esses fundos

---

## 4. Score por gestora em RF

**Decisão atual:** Em MM, a aba Gestoras mostra um score médio por gestora
calculado via `scorePool(allFunds)`. Em RF isso seria matematicamente
incoerente — mistura D0, Crédito, Incentivadas, FIDCs e Internacionais
(produtos não comparáveis). Solução temporária na Fase 1: omitir o score,
mostrar apenas dados qualitativos (nome, AUM somado dos fundos da gestora
no universo RF, lista de fundos sob gestão, texto qualitativo do
gestoras.json).

**Por que adiar:** o tratamento correto requer reescrita parcial da aba —
calcular score-por-gestora dentro de cada subgrupo separadamente, e
permitir que uma gestora apareça múltiplas vezes (uma vez por subgrupo
onde ela tem fundos). Não cabe na Fase 1.

**Quando revisitar:** após a Fase 1 estar em produção e termos feedback
de uso da aba Gestoras de RF. Considerar também harmonizar com MM se a
aba Gestoras do MM for refatorada.

**Onde mexer:**
- `dashboard_rf.html` — funções `getGestoraScore`, `renderGestorasList`,
  `openGestora` (sub-cards de score)

---

## 5. Aba Histórico desabilitada na Fase 1

**Decisão atual:** A aba Histórico do MM mostra a evolução da carteira
recomendada ao longo do tempo (lê `data/historico_carteira.json`). No
dashboard RF a aba foi escondida — a carteira recomendada de RF está
sendo construída agora e ainda não há histórico a exibir.

**Por que adiar:** sem snapshots mensais do `estado_rf.json` (que não
existem ainda), não há série temporal a renderizar. Não há valor em
mostrar uma aba vazia ou sintética.

**Quando revisitar:** depois que houver pelo menos 6 meses de snapshots
da carteira recomendada de RF acumulados.

**Onde mexer:**
- Pipeline novo: snapshot mensal do `estado_rf.json` para
  `data/historico_carteira_rf.json` (estrutura espelhada da do MM)
- `dashboard_rf.html` — desesconder o `<div class="tab" data-tab="historico">`
  e reapontar o fetch de `historico_carteira.json` para `historico_carteira_rf.json`

---

## 6. Inf/NaN nas funções shared (`calcular_sortino`, `calcular_excesso_anualizado`)

**Decisão atual:** O pipeline RF sanitiza inf/NaN em `gerar_fundos_rf_json`
substituindo por `None` antes de serializar o JSON, e loga `[JSON_SANITIZE]`
com os fundos afetados. Resolve o problema observado durante o smoke
test do Bloco A da Etapa 1.3, onde 3 fundos produziam `Infinity` em
`sortino_24m/36m` e `excesso_36m` — quebrando o `JSON.parse` do dashboard.

**Problema:** o root cause está nas funções de cálculo compartilhadas
com o pipeline MM (`scripts/pipeline_fundos.py`):
- `calcular_sortino` divide `excesso.mean()` por `downside_dev`. Se
  `downside_dev` é minúsculo mas não-zero (D0 / fundos de vol baixíssima),
  o resultado overflowea para `inf`.
- `calcular_excesso_anualizado` (no RF) tem `base ** (1/n_anos) - 1`. Em
  edge cases (retorno cumulativo bizarro), `base` pode ficar inf.
- `calcular_sharpe` tem o mesmo padrão de Sortino mas com `vol` (já tem
  guard de `vol == 0`); poderia também produzir inf em casos extremos.

**Por que adiar a correção upstream:** mexer em `pipeline_fundos.py` afeta
o MM em produção. Não cabe no escopo da Fase 1 do RF. A sanitização no
RF é defensiva e suficiente.

**Quando revisitar:** ao consolidar tratamento de "métrica sem dados"
nos dois pipelines (já alinhado com BACKLOG #1).

**Onde mexer (consertando upstream):**
- `scripts/pipeline_fundos.py` — adicionar `if not np.isfinite(result):
  return None` em `calcular_sortino`, `calcular_sharpe`, `calcular_calmar`
- Eliminar a sanitização local do `gerar_fundos_rf_json` quando upstream
  estiver limpo

**Fundos que dispararam na primeira rodada do Bloco A:**
- Trend Pós-Fixado FIC FIRF Simples (D0): `sortino_24m`, `sortino_36m`
- BRB FIRF IMA-S LP (D0): `sortino_36m`, `excesso_36m`
- Ouro Preto FIC de FIDC (FIDCs): `excesso_36m`
