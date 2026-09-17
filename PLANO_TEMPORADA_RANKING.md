# Plano — Temporada 1 / Ranking geral com prêmio de R$1.000

Status: **implementado em 2026-09-17** (commit "Temporada / ranking geral"). Este arquivo fica como referência das regras. Diferenças em relação ao plano original: `temporada_id` é preenchido no **sorteio do chaveamento** (não na criação) — assim campeonato criado antes da temporada abrir também entra, se for sorteado com ela aberta; a temporada é criada e encerrada por uma tela de admin (`/admin/temporadas`), não por comando.

Decisões já fechadas com o Marcelo estão marcadas ✅. Onde eu assumi algo pra fechar o plano, está marcado ⚠️ — checar antes de começar a implementar.

---

## 1. O que foi decidido

- ✅ **Temporada 1 / Season 1**: primeira edição do ranking geral do site (não por federação).
- ⚠️ **Duração**: o Marcelo falou "temporada 6" — estou assumindo **temporada de 6 meses** (provável perda de palavra na transcrição de voz). Confirmar a data de início/fim exatas antes de criar a Temporada 1 no banco.
- ✅ **Pontuação por campeonato**: campeão = 3 pontos, vice = 2 pontos, 3º lugar = 1 ponto, os demais = 0.
- ✅ **Jogo de 3º lugar de verdade** (não dividir o ponto entre os dois semifinalistas perdedores) — porque tem campeonato que paga prêmio de 3º lugar, então precisa de um resultado único e oficial.
- ✅ **Prêmio de R$1.000 pro 1º lugar do ranking geral no fim da temporada**, condicionado a bater uma meta de participação: **se a temporada não chegar a 100 times, não sai o prêmio em dinheiro** (o ranking em si continua existindo). Serve de incentivo pros próprios times chamarem outros times.
- ✅ **Anti-farm**: só conta ponto de campeonato **pago** (`valor_inscricao > 0`) e **cheio** (fechou com o número máximo de vagas, sem começar com bye/W.O. de vaga vazia — ver o recurso "começar com menos times" que acabamos de lançar). Campeonato grátis ou que começou incompleto nunca gera ponto.

## 2. Coisas que eu decidi por conta própria (bom senso) e proponho manter

- **"100 times" = 100 times distintos com pelo menos uma inscrição paga num campeonato elegível durante a temporada.** Não é "100 contas cadastradas" — cadastro é grátis e daria pra fraudar a meta sem ninguém pagar nada. Tem que ser gente pagando de verdade, porque é isso que justifica o prêmio.
- Se a meta não for batida, o ranking e o "campeão da temporada" continuam sendo registrados e mostrados (histórico/hall da fama) — só o dinheiro que não sai.
- A elegibilidade do campeonato pro ranking (pago + cheio) é **travada no momento do sorteio do chaveamento**, não recalculada depois — mesmo princípio já usado no ajuste de prêmio por bye.
- Pontos ficam guardados numa tabela própria (ledger), não só uma soma num campo do time — assim cada time consegue ver o extrato exato de onde ganhou cada ponto, e o resultado de uma temporada fechada nunca muda depois.
- Pagamento do prêmio da temporada é **manual** (o admin do site confirma, como já acontece com o prêmio de campeonato) — R$1.000 é valor alto pra sair sozinho sem alguém conferir antes.
- Critério de desempate pro 1º lugar (precisa estar escrito nos termos de uso **antes** de qualquer temporada fechar, pra não gerar disputa com dinheiro real):
  1. Mais campeonatos vencidos na temporada
  2. Mais vices
  3. Menos partidas jogadas (eficiência)
  4. Persistindo o empate: decisão do admin do site, registrada publicamente

## 3. Modelo de dados (tudo aditivo — nenhuma coluna/tabela existente é removida ou renomeada)

### Tabela nova `Temporada`
- `id`
- `numero` (Integer) — 1, 2, 3...
- `nome` (String) — "Temporada 1"
- `data_inicio`, `data_fim` (Date)
- `meta_times` (Integer, default 100)
- `valor_premio` (Float, default 1000.00)
- `status` (String) — `aberta` | `encerrada`
- `premio_pago_status` (String nullable) — mesmo vocabulário já usado em `Torneio.premio_status`: `concluido` | `pendente` | `falhou` | `sem_meta` (novo valor, quando não bateu os 100 times)
- `premio_transferencia_id` (String nullable)
- `time_campeao_id` (FK Team, nullable) — trava o 1º lugar no momento em que a temporada fecha

### Tabela nova `PontuacaoRanking` (ledger)
- `id`
- `temporada_id` (FK Temporada)
- `torneio_id` (FK Torneio)
- `time_id` (FK Team)
- `colocacao` (String) — `campeao` | `vice` | `terceiro`
- `pontos` (Integer)
- `criado_em`

### `Torneio` ganha 2 colunas
- `temporada_id` (FK Temporada, nullable) — preenchido sozinho com a temporada `aberta` no momento do sorteio do chaveamento (fica `None` se não tiver nenhuma temporada rodando)
- `elegivel_ranking` (Boolean, default `False`) — calculado e travado no sorteio do chaveamento: `True` só se `valor_inscricao > 0` **e** `times_pagos == max_times` naquele momento

### `Confronto` ganha 1 coluna
- `eh_disputa_3_lugar` (Boolean, default `False`) — marca o confronto especial que não alimenta a fase seguinte. Reaproveita o campo `fase` já existente com o valor `"Disputa de 3º lugar"` pra exibição.

## 4. Regra de geração do jogo de 3º lugar

Gatilho: o mesmo momento em que hoje as duas Semifinais terminam e o site monta a Final.

- Se as duas Semifinais tiveram um vencedor de verdade (nenhuma delas foi W.O. por falta de adversário) → cria a "Disputa de 3º lugar" entre os dois perdedores, em paralelo à Final.
- Se uma Semifinal foi W.O. por falta de adversário (só acontece em campeonato de 3 times numa chave de 4 — o time que sobrou nunca jogou essa fase) → não tem o que disputar: o único perdedor de semifinal real já fica em 3º automaticamente, sem partida.
- Campeonato com chave de 2 (Final direto, sem Semifinal) → não existe 3º/4º lugar possível, não gera nada.
- **Atenção ao implementar**: essa é a parte que mais mexe no motor de chaveamento que acabamos de lançar (bracket com bye). Repetir a mesma bateria de testes de bracket (2 a 32 times) cobrindo agora também a geração da disputa de 3º lugar em cada tamanho.

## 5. Onde aparece no site

- **`/ranking`** (página nova): nome/contagem regressiva da temporada atual, barra de progresso "X / 100 times" (deixa claro se o prêmio está garantido ou não), Top 10 em destaque, tabela completa paginada embaixo (todo mundo consegue ver sua posição e quanto falta pro Top 10). Cada linha da tabela mostra, além da posição e dos pontos: **quantidade de títulos** (`colocacao='campeao'`), **quantidade de vices** (`colocacao='vice'`) e **quantidade de campeonatos jogados** na temporada (contagem de campeonatos `elegivel_ranking=True` em que o time teve inscrição paga — não só os que ele venceu). As 3 primeiras colunas vêm direto de somar/contar linhas de `PontuacaoRanking`; a última precisa contar participação em `Inscricao`/`Torneio`, não só linhas do ledger de pontos.
- **Perfil do time**: extrato "pontos ganhos na Temporada 1", listando cada campeonato e a colocação (usa a `PontuacaoRanking`).
- **`/ranking/historico`**: temporadas encerradas, campeão de cada uma, se o prêmio saiu ou não. Vale montar isso desde já mesmo simples, porque a Temporada 1 um dia vai fechar.
- **Chaveamento**: caixinha visual nova pra "Disputa de 3º lugar", separada da progressão principal (ao lado/abaixo da Final).
- Selo/atalho na home linkando pro ranking.

## 6. Pagamento do prêmio da temporada

- Mesma infraestrutura já usada nos prêmios de campeonato (`pagamentos.enviar_pix`), pro capitão do time — mesma trava de precisar ter chave Pix cadastrada (mesmo aviso que já existe hoje pro prêmio de campeonato).
- Desconta a taxa fixa da AbacatePay (mesma lógica de "prêmio + taxa" já usada, pra chegar exatamente R$1.000 na conta do time).
- Confirmação manual do admin do site ao fechar a temporada, com tela de "tem certeza?" — não sai sozinho.

## 7. Casos de borda pra cobrir no teste (antes de subir, como sempre)

- Campeonato de 2 times (chave de 2): sem 3º lugar, sem quebrar nada.
- Campeonato de 3 times numa chave de 4: 3º lugar "automático" sem partida.
- Campeonato grátis (`valor_inscricao = 0`): nunca gera ponto, mesmo cheio.
- Campeonato pago mas que começou incompleto (bye): nunca gera ponto, mesmo pago.
- Time que dissolve no meio da temporada (capitão saiu, era o último membro): os pontos já creditados continuam no ledger (já aconteceram), o time só some da lista de "times ativos".
- Duas temporadas não podem se sobrepor — travar isso na criação da Temporada 2 pra frente.
- Contestação de placar depois que os pontos já foram creditados: a regra que já existe (contestação só durante o campeonato, ver termos de uso) já evita isso — só confirmar que continua valendo aqui.

## 8. Fora de escopo deste plano

Fica registrado no roadmap geral (memória do projeto), não misturar na execução deste plano:
- Seletor de FC 26 / FC 27
- Diretório de times (`/times`)
- Mercado de transferências

## 9. Ordem sugerida de implementação

1. Modelo de dados (`Temporada`, `PontuacaoRanking`, colunas novas em `Torneio`/`Confronto`) + migração aditiva no padrão já usado (`_garantir_colunas_novas`).
2. Lógica do jogo de 3º lugar na geração/avanço do chaveamento — reaproveitar e testar de novo o motor de bracket com bye (seção 4).
3. Cálculo e trava de `elegivel_ranking` + criação das linhas de `PontuacaoRanking` ao finalizar um campeonato.
4. Criar a Temporada 1 (comando/seed direto, não precisa de tela de admin nesse primeiro momento) — confirmar datas exatas antes (ver item ⚠️ na seção 1).
5. Páginas `/ranking`, `/ranking/historico` e o extrato no perfil do time.
6. Fechamento de temporada + pagamento manual do prêmio.
7. Atualizar termos de uso com a regra de desempate e a condição da meta de 100 times.
8. Testar de ponta a ponta como sempre: simular uma temporada inteira localmente (vários campeonatos — cheio/incompleto, pago/grátis, bracket pequeno/grande, com e sem 3º lugar) antes de subir pra produção.
