# Plano — Sistema de XP e nível (jogador e time)

Status: **planejado, nada implementado ainda**. Escrito em 2026-09-17 pra ser executado depois, possivelmente por outra sessão/modelo — por isso está detalhado o suficiente pra não depender da conversa que o gerou. Ver também [PLANO_TEMPORADA_RANKING.md](PLANO_TEMPORADA_RANKING.md) — este sistema reaproveita a mesma infraestrutura.

## 1. Diferença importante em relação ao ranking da Temporada

São dois sistemas com propósitos diferentes, não confundir:

- **Ranking (Temporada 1)**: mede quem está no topo *agora*, reseta a cada temporada, tem prêmio em dinheiro (R$1.000) e por isso tem travas rígidas anti-farm (só campeonato pago e cheio conta).
- **XP / nível**: mede a *carreira* do jogador e do time, **acumula pra sempre, nunca reseta**, não tem prêmio em dinheiro associado — é reputação/gamificação. Por não envolver dinheiro, pode ser mais permissivo nas regras (ver seção 3).

## 2. Duas trilhas independentes

- **XP do jogador** (`User`) — pertence à pessoa, não ao time. **Ponto central pedido pelo Marcelo**: se o jogador trocar de time, ele mantém o nível dele — é isso que permite "saber que ela é boa" mesmo num time novo. Por isso o XP tem que morar no perfil do jogador, nunca no time.
- **XP do time** (`Team`) — representa a carreira do clube em si. Não se transfere quando um jogador entra ou sai.

As duas trilhas crescem juntas nos mesmos eventos (ver seção 3), só não se misturam nunca.

## 3. Como se ganha XP (reaproveita os mesmos 2 gatilhos do ranking)

- **Participação** — toda partida validada com resultado real (não W.O.): **+10 XP** pro time vencedor, **+4 XP** pro time perdedor, creditado ao time e a cada jogador que é membro dele nesse momento. Vale em **qualquer** campeonato, pago ou grátis, cheio ou não — sem trava anti-farm aqui, porque XP não paga nada.
- **Colocação em campeonato** — campeão **+100 XP**, vice **+60 XP**, terceiro **+30 XP**, creditado ao time e a cada membro atual. Essa parte **reaproveita a trava de `Torneio.elegivel_ranking`** (pago + cheio) do plano da Temporada — pra "eu sou campeão" continuar significando alguma coisa, mesmo sem dinheiro envolvido.
- **Simplificação assumida (limitação conhecida, documentar)**: o site não sabe quem especificamente jogou dentro de uma partida — só sabe o resultado do time. Por isso o XP de participação/colocação vai pra **todo mundo que é membro do time no momento do evento**, não só quem "jogou de verdade". Se isso incomodar no futuro, dá pra evoluir pra presença por partida (precisaria de uma tabela nova de "quem confirmou presença"), mas é bem mais trabalho — não fazer isso na v1.

## 4. Nível

- Guardar só `xp_total` (Integer, acumulado) — **não** guardar um "nível" fixo calculado no banco. Calcular o nível na hora, a partir da fórmula, assim dá pra ajustar a curva de dificuldade depois sem precisar de migração nem recalcular nada.
- Fórmula sugerida (ajustável): nível N exige `50 × N × (N+1)` de XP acumulado — cresce devagar no começo, mais puxado depois. Vale recalibrar depois de ver quanto XP um jogador ativo típico ganha por mês.

## 5. Onde aparece

- Selo "Nível X" no perfil do jogador, e ao lado do nick em telas onde já aparece o jogador hoje (chaveamento, tela de partida, e futuramente o diretório de times).
- Selo "Nível X" na página do time.
- **Uso principal**: alimenta o mercado de transferências — quem for recrutar consegue ver o nível do jogador antes de chamar, em vez de decidir às cegas.

## 6. Modelo de dados (tudo aditivo)

- `User.xp_total` (Integer, default 0) — coluna nova
- `Team.xp_total` (Integer, default 0) — coluna nova
- Sem tabela de ledger separada na v1 (diferente do ranking): não tem dinheiro envolvido nem precisa de auditoria linha a linha, soma direta já basta. Se um dia quiser um extrato tipo "de onde vieram meus XP" no perfil, dá pra adicionar uma tabela parecida com `PontuacaoRanking` depois — não bloqueia a v1.

## 7. Fora de escopo v1

- Rastrear individualmente quem jogou dentro de cada partida (attendance por partida)
- Conquistas/badges especiais (ex.: "5 campeonatos seguidos")
- Decaimento de XP por inatividade

## 8. Por que construir isso logo depois da Temporada/Ranking

Reaproveita exatamente os mesmos dois gatilhos que o ranking já vai ter (partida validada, campeonato finalizado com colocação) — é a extensão mais barata de toda essa fila, o código que dispara os dois sistemas fica lado a lado no mesmo lugar.
