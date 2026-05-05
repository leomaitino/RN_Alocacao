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
