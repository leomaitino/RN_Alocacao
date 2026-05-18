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

---

## 7. Validação de senha é só client-side (compartilhado com MM)

**Decisão atual:** Hoje a validação de senha acontece em `checkPassword()`
no frontend (`dashboard_rf.html` e `dashboard.html`). Requisições POST
diretas (curl, Postman, qualquer cliente HTTP) podem salvar
recomendados/pesos/alocações sem senha alguma — os endpoints
`/api/save-*-rf` aceitam o body sem checar campo `senha`.

**Por que existe:** mirror exato do padrão MM em produção. Não é
regressão da Fase 1 — o problema já existe no MM hoje.

**Por que adiar:** o usuário típico está sempre passando pelo dashboard,
onde a validação client-side cobre 99% dos casos. Atacar agora exigiria
mexer em MM + RF simultaneamente, o que não cabe na Fase 1.

**Quando revisitar:** junto com outras melhorias de segurança do servidor.

**Solução envolve:**
- (a) Servidor verificar o campo `senha` no payload de cada rota POST e
  retornar 401 se inválido.
- (b) Padronizar com o MM para não criar divergência entre as duas
  pipelines de autenticação.
- (c) Considerar token-based auth (JWT) ou rate limiting se o uso do
  AlphaDesk crescer para fora da equipe pequena atual.

**Onde mexer:**
- `servidor.py` — todas as rotas `/api/save-*` (MM e RF)
- `dashboard.html` + `dashboard_rf.html` — passar a senha no body do POST
  (hoje validam só client-side e enviam sem senha)

---

## 8. Snapshot mensal de estado_rf.json para alimentar futura aba Histórico

**Decisão atual:** A aba Histórico do dashboard RF está escondida (BACKLOG
#5) porque não há série temporal de snapshots da carteira recomendada.
Sem coletar esses snapshots a partir de agora, o BACKLOG #5 nunca
destrava — sempre faltarão os "6 meses acumulados" que ele exige.

**Solução proposta:** criar pipeline de snapshot mensal que copia
`data/estado_rf.json` para `data/snapshots_estado_rf/AAAA-MM.json` (por
exemplo `2026-06.json` no fim de junho). Acumula histórico sem nenhuma
mudança no dashboard nem no fluxo de uso normal.

**Implementação possível:**
- (a) Cron mensal no servidor (Render.com tem cron jobs no plano pago,
  ou um job externo no GitHub Actions).
- (b) Commit manual no fim do mês (script `python scripts/snapshot_rf.py`
  que copia o arquivo e gera commit). Mais simples, depende de disciplina.
- (c) Hook no `/api/save-recomendados-rf` que detecta virada de mês e
  faz o snapshot automaticamente antes do save. Mais robusto, sem
  depender de cron externo.

**Por que registrar agora:** se começarmos a fazer snapshots desde já,
em 6 meses já teremos massa crítica para destravar #5. Se postergar a
decisão, postergamos também a reativação da aba.

**Onde mexer:**
- Novo: `scripts/snapshot_estado_rf.py` (job de cópia)
- Novo: `data/snapshots_estado_rf/` (diretório versionado com 1 arquivo
  por mês)
- Eventualmente: `servidor.py` se for usar abordagem (c)

---

## 9. Mesmo bug (cotas defasadas pelo mês corrente) existe no pipeline MM

**Decisão atual:** No pipeline RF, o bug foi corrigido — `gerar_cotas_rf_json`
agora aceita `df_cotas` em memória (preferência) com fallback para parquets.
Resultado: `cotas_rf.json` inclui o mês corrente (até 2026-05-15 em maio/2026
em vez de parar em 2026-04-30). Ver commit `fix(rf): include current-month
CVM data in cotas_rf.json`.

**Problema:** A lógica análoga em `pipeline_fundos.py` (MM) tem a mesma
falha:
- A geração de `cotas.json` lê apenas dos parquets cacheados (etapa 7 do
  `salvar_outputs`).
- `baixar_informes_cvm` não cacheia o mês corrente por design
  (`if not eh_mes_atual: chunk.to_parquet(...)`) — para sempre re-baixar
  o mês em andamento e pegar dados novos publicados durante o dia.
- Resultado: `cotas.json` em produção fica defasado em até 2-4 semanas.

**Por que passou despercebido:** O dashboard MM mostra retornos acumulados
de longa data (5 anos). Numa série de 1300 pontos, perder 10-20 pontos
recentes é visualmente imperceptível. Só ficou óbvio no RF porque a gente
foi verificar explicitamente a última data por fundo.

**Por que adiar:** mexer no `pipeline_fundos.py` afeta produção do MM
imediatamente. Cabe numa janela de manutenção planejada do pipeline MM,
não num hotfix isolado.

**Quando revisitar:** próxima rodada de manutenção do pipeline MM —
provavelmente junto com a correção do `or 0` no Sharpe (BACKLOG #1) e
a sanitização inf/NaN upstream (BACKLOG #6).

**Como aplicar o fix (~30 linhas):**
- Refatorar a função que gera `cotas.json` em `pipeline_fundos.py`
  (atualmente inline no `salvar_outputs`) para aceitar `df_cotas` como
  parâmetro opcional.
- Passar o `df_cotas` em memória do `main()` para essa função.
- Manter fallback para parquets (compatibilidade quando rodando
  `--sem-cvm` ou em ambientes sem rede).

**Onde mexer:**
- `scripts/pipeline_fundos.py` — `salvar_outputs` (etapa 7 inline).
  Extrair em função `gerar_cotas_json(fundos_list, cache_cvm_dir,
  df_cotas=None)` espelhando o padrão do RF.

---

## 10. IMA-B longo defasado no benchmarks.json

**Decisão atual:** O IMA-B longo (chave `IMA-B` em `benchmarks.json`)
está em 2026-04-02 (46 dias atrás) na rodada de 2026-05-18. O pipeline
RF não baixa essa série — `montar_e_atualizar_benchmarks` agora
SOBRESCREVE CDI e IPCA+spreads com dados frescos (fix do commit
`fix(rf): overwrite stale benchmarks`), mas PRESERVA `IMA-B` porque o
RF não tem fonte para essa série (nenhum fundo de RF usa IMA-B longo
como referência — Inflação/Incentivadas usam IMA-B 5).

**Problema:** o MM usa IMA-B longo para alguns fundos macro/inflação e
portanto sofre a defasagem em produção. A chave `IMA-B` no
`benchmarks.json` compartilhado fica congelada na data da última rodada
do pipeline MM, que pode ser semanas atrás.

**Por que adiar:** o RF não depende dessa chave; ela só está no JSON
porque o MM herda do mesmo arquivo. Fix cabe na próxima manutenção do
pipeline MM, não em hotfix RF.

**Solução:**
- (a) Adicionar arquivo `input/IMAB-HISTORICO.xlsx` versionado (parecido
  com `IMAB5-HISTORICO.xlsx`) e fazer o pipeline MM carregá-lo. Mesmo
  padrão. Usuário baixa da Anbima quando quiser atualizar.
- (b) Aplicar no `pipeline_fundos.py` (MM) a mesma lógica de sobrescrever
  do RF — fonte do IMA-B longo passa a alimentar o JSON em cada rodada
  do MM em vez de depender de inicialização única.

**Onde mexer:**
- `scripts/pipeline_fundos.py` — `montar_benchmarks` ou função
  equivalente que escreve `benchmarks.json`.
- Eventualmente `input/IMAB-HISTORICO.xlsx` para fonte fresca.

---

## 11. IHFA defasado por bloqueio de bot da Anbima

**Decisão atual:** IHFA em 2026-03-31 (48 dias atrás). Pipeline tenta 3
URLs conhecidas mas Anbima responde 403 / página HTML em vez de CSV. RF
preserva a chave (mesma decisão do IMA-B longo).

**Problema:** o MM usa IHFA como benchmark de comparação multimercados.
Em produção o gráfico do MM mostra IHFA atrasado.

**Por que adiar:** não afeta RF (nenhum benchmark RF é IHFA).

**Solução possível:**
- (a) Baixar manualmente o CSV/Excel do IHFA da Anbima e versionar em
  `input/IHFA-HISTORICO.xlsx`. Pipeline carrega via `--ihfa
  caminho.csv` (suporte já existe no MM). Cadência: mensal ou semanal,
  conforme apetite.
- (b) Aceitar que o IHFA fica defasado até alguém atualizar manualmente.
  Documentar no README a frequência esperada.

**Onde mexer:**
- `scripts/pipeline_fundos.py` — não muda código, só o caminho passado
  pra `baixar_ihfa(caminho_local=...)`.
- Operacional: agendar download manual da Anbima.
