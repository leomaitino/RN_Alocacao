# Carteira Simulada de Fundos Listados — especificação da regra

Resumo da lógica da aba **Simulação** do `dashboard_listados.html`, escrita
para ser implementada por um modelo de compra automática. O pipeline publica
a carteira-alvo pronta em `data/listados_sinal.json` (seção 6), então o modelo
pode consumir o feed em vez de recalcular.

## 1. Universo

Fundos listados de renda fixa com `status = "Aprovado"` em
`data/listados_universo.json` (hoje 13: CDII11, IFRI11, IFRA11, JURO11, KDIF11,
BINC11, AZIN11, DIVS11, KNCR11, KNIP11, XPCI11, MCCI11, MXRF11). O universo é
editado manualmente; o feed informa a lista vigente em `regra.universo_tickers`.

## 2. Dados de entrada (por fundo, diários)

| Dado | Fonte | Frequência real |
|---|---|---|
| Preço de fechamento `P_t` | B3 via Yahoo Finance (`TICKER.SA`) | diária |
| Valor patrimonial por cota `VP_t` | CVM: informe diário (FI-Infra), informe mensal (FII e Fiagro), informe quadrimestral (FIP-IE) | diária / mensal / 4 meses — mantido constante (ffill) até o informe seguinte |
| `P/VP_t = P_t / VP_t` | calculado | diária |
| Proventos (data ex) | Yahoo Finance | por evento — usados só para medir retorno, não para o sinal |

Séries completas em `data/listados_series.json` (`datas`, `preco`, `vp`, `pvp`, `dividendos`).

## 3. Regra de elegibilidade

Na data de decisão `d`, usando o **pregão anterior** `t = d − 1` (sem look-ahead):

1. `média_t` = média aritmética de `P/VP` nas datas `u` com `t − 12 meses ≤ u ≤ t`
   (janela móvel de 12 meses, inclusiva). Parâmetro `janela_meses` (6, 12 ou 24).
2. Exige pelo menos **60 observações** de `P/VP` na janela (`min_obs`); senão,
   o fundo é inelegível ("histórico curto").
3. Fundo elegível ⇔ **`P/VP_t < 1,00`** **e** **`P/VP_t < média_t`**.

Motivos de exclusão, nesta ordem: sem P/VP → histórico curto → P/VP ≥ 1,00 →
P/VP ≥ média.

## 4. Construção da carteira

- **Pesos iguais** entre os elegíveis: `w_i = 1 / n`.
- **Sem elegíveis** → 100% em caixa remunerado a CDI.
- **Rebalanceamento mensal** (padrão): a elegibilidade é reavaliada no **primeiro
  pregão de cada mês** com os dados do pregão anterior; a composição fica fixa
  até o próximo mês. Alternativa `diário`: reavaliação todo pregão (mais giro).
- Entradas e saídas acontecem só nas datas de rebalanceamento. Um fundo que
  perde a condição no meio do mês permanece até o rebalanceamento seguinte.
- Dentro do período, a simulação usa a média dos retornos diários dos fundos
  na carteira (equivale a pesos iguais rebalanceados diariamente). Um operador
  real que só rebalanceia no mês terá pequeno desvio por deriva de pesos.

## 5. Medição de retorno (apenas para avaliação, não afeta o sinal)

- Retorno diário por fundo: `(P_t + provento_ex_t) / P_{t−1} − 1`.
- Carteira: média dos retornos diários dos fundos na carteira, acumulada
  em base 100. Caixa: retorno diário do CDI (`data/benchmarks.json`).
- Gross-up de IR opcional para comparar com o CDI bruto: retorno diário ÷ 0,85
  (premissa PF, alíquota 15%). É só comparação; o sinal não usa.
- Resultado em 06/10/2026 (universo Aprovados, 12M, mensal, 24 meses, gross-up):
  simulada +42,0% vs Carteira Sugerida +27,9% vs CDI +29,4%; média de 3,7
  fundos na carteira; 20% do tempo em caixa; 17 rebalanceamentos.

## 6. Feed para o modelo: `data/listados_sinal.json`

Gerado a cada execução de `python scripts/pipeline_listados.py` e servido em
`https://rn-alocacao.onrender.com/data/listados_sinal.json`.

```json
{
  "gerado_em": "2026-10-06T14:10:00",
  "data_referencia": "2026-10-06",           // último pregão usado no sinal
  "regra": {"universo": "Aprovado", "janela_meses": 12, "min_obs": 60,
            "rebalanceamento": "mensal", "universo_tickers": ["CDII11", "..."]},
  "proximo_rebalanceamento": "2026-11-02",    // primeiro dia útil do mês seguinte
  "n_elegiveis": 9,
  "elegiveis": ["XPCI11", "CDII11", "..."],
  "pesos_alvo": {"XPCI11": 0.111111, "...": 0.111111},
  "caixa_pct": 0.0,
  "fundos": [ {"ticker": "XPCI11", "tipo": "FII", "preco": 80.68, "vp_cota": 87.61,
               "pvp": 0.9209, "media_pvp": 0.931, "n_obs": 248,
               "desconto_vs_media": -0.011, "elegivel": true, "motivo": "ok",
               "vp_fonte": "CVM informe mensal FII", "dy_12m": 0.1325}, ... ],
  "avisos": ["..."]
}
```

Protocolo sugerido para o operador:

1. No primeiro pregão do mês (ou em `proximo_rebalanceamento`), rodar o pipeline
   (ou baixar o feed já publicado) e ler `elegiveis` + `pesos_alvo`.
2. Vender o que saiu, comprar o que entrou, igualar pesos; `caixa_pct = 1`
   significa zerar posições e ficar em CDI.
3. Registrar `data_referencia` e `gerado_em` da ordem para auditoria.
4. Ignorar o feed se `data_referencia` estiver a mais de 5 pregões da data
   atual (pipeline não rodou) ou se `n_obs` de um fundo for inferior a 60.

## 7. Cuidados e limitações

- **Rodar após o fechamento**: se o pipeline roda durante o pregão, o Yahoo
  entrega o preço intradiário do dia como última cotação e o sinal usa esse
  valor. Fundos perto do limiar mudam de lado ao longo do dia (em 06/10/2026,
  KNIP11, JURO11 e MXRF11 eram elegíveis de manhã e deixaram de ser à tarde).
  Para o rebalanceamento, gerar o feed depois das 18h do pregão anterior ou
  antes da abertura.
- **VP defasado**: FII/Fiagro só atualizam VP no informe mensal da CVM (com
  ~1 mês de atraso) e o AZIN11 (FIP-IE) a cada 4 meses. O P/VP "diário" entre
  informes usa o último VP conhecido. Para esses, a condição `P/VP < 1` pode
  virar com o próximo informe sem o preço ter mudado.
- **Dados do Yahoo** podem ter lacunas de pregões ou proventos com casa
  decimal errada (o pipeline corrige os casos detectáveis e loga
  `[PROVENTO_FIX]`). O sinal depende só de preço e VP.
- **Sem custos**: corretagem, emolumentos, spread, lotes mínimos e liquidez
  (alguns FI-Infra giram < R$ 300 mil/dia) não entram na simulação. Com
  rebalanceamento diário o giro cresce muito.
- **Viés de amostra**: o backtest cobre 2024–2026, período em que os FI-Infra
  abriram desconto (MP 1303, meados de 2025) e depois recuperaram; a regra
  compra exatamente nesses momentos. Não há garantia de repetição.
- **Concentração**: com poucos elegíveis a carteira fica concentrada (média
  de 3,7 fundos no backtest; mínimo 0). Considerar um teto de peso por fundo
  ou um número mínimo de fundos antes de operar.
- **Stress**: a regra não olha os alertas de crédito (aba Carteira & Stress).
  XPID11 e IRIM11 não estão no universo Aprovados, mas se o universo mudar vale
  excluir fundos com `stress_nivel = 3` (campo em `data/listados.json`).
- O sinal é informativo. A decisão e a execução das ordens são do operador.

## 8. Parâmetros que o modelo pode variar

| Parâmetro | Padrão | Alternativas no dashboard |
|---|---|---|
| Universo | Aprovados | Painel (22), Painel + Sugeridos (32), Todos (50) |
| Janela da média | 12 meses | 6, 24 |
| Rebalanceamento | mensal | diário |
| Mínimo de observações | 60 pregões | fixo |
| Gross-up (só avaliação) | ligado, 15% | desligado |

Para gerar o feed com outros parâmetros, alterar `SINAL_PARAMS` em
`scripts/pipeline_listados.py`.
