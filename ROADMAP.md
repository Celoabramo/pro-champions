# Roadmap Pro Champions — visão geral e ordem recomendada

Status: itens 1 a 4 implementados em 2026-09-17; 5 e 6 ainda planejados. Este arquivo é o ponto de entrada — se você (Claude, ou outra sessão) for executar algo desse roadmap, comece por aqui. Escrito em 2026-09-17.

Cada item abaixo tem uma descrição rápida; os três maiores têm plano detalhado em arquivo separado. A ordem é a recomendada e tem uma razão específica: **cada peça deixa a próxima mais valiosa ou mais barata de construir** — não é só "o mais fácil primeiro".

## Ordem recomendada

### 1. Seletor FC 26 / FC 27 — ✅ feito (2026-09-17)
Reaproveita o padrão que já existe pro campo `Torneio.geracao` (antiga/nova geração de console) pra um campo novo `Torneio.jogo` ("fc26"/"fc27"). Migração aditiva, default "fc26" pros campeonatos existentes. Pequeno, sem dependência de mais nada — pode ir primeiro. Detalhe: os 3 textos hoje fixos em "FC 26" em `templates/confronto_detalhe.html` precisam virar dinâmicos. Decisão em aberto: deixar criar campeonato de FC 27 antes do lançamento (com aviso) ou travar até a data — perguntar ao Marcelo quando for implementar.

### 2. Diretório de times (`/times`) — ✅ feito (2026-09-17)
Página pública listando todo time cadastrado (escudo, tag, capitão, membros), filtro por federação. Não precisa de tabela nova — é leitura do que já existe. É a "prateleira" onde os itens 3, 4 e 5 vão se apoiar (mostrar pontos, nível, vaga de recrutamento).

### 3. Temporada 1 / Ranking geral — ✅ feito (2026-09-17) — ver [PLANO_TEMPORADA_RANKING.md](PLANO_TEMPORADA_RANKING.md)
Ranking geral do site por temporada (reseta), pontuação por pódio (campeão 3 / vice 2 / terceiro 1), jogo de 3º lugar de verdade no chaveamento, só conta campeonato pago e cheio (anti-farm), prêmio de R$1.000 condicionado a bater 100 times participando. **É a única peça deste roadmap que mexe em dinheiro real** — a que precisa de mais cuidado de teste antes de subir.

### 4. XP e nível — ✅ feito (2026-09-17, XP = reais ganhos em prêmio; prêmios de vice/3º junto) — ver [PLANO_XP_NIVEL.md](PLANO_XP_NIVEL.md)
Progressão de carreira (não confundir com o ranking da temporada, que reseta): XP acumula pra sempre, por jogador (perfil, segue a pessoa quando ela troca de time) e por time, separadamente. Reaproveita os mesmos dois gatilhos que o ranking já vai ter (partida validada, campeonato finalizado) — por isso é a extensão mais barata da fila, construir logo depois do item 3.

### 5. Mercado de transferências
Capitão marca o time como "recrutando" (+ mensagem); jogador manda pedido de entrada (notificação pro capitão, reaproveitando o sistema de `Notificacao` que já existe); capitão aceita ou recusa; entrada usa a mesma trava de hoje (1 time por jogador, mesma lógica de `entrar_time`/`sair_time`). Não inventa regra nova de posse de time. Com o item 4 pronto, quem for recrutar já vê o nível do jogador antes de chamar — não decide às cegas.

### 6. Escalação e posições — ver [PLANO_ESCALACAO_POSICOES.md](PLANO_ESCALACAO_POSICOES.md)
Cada jogador ocupa um slot específico do esquema tático do time (`Team.formacao`), um slot só pode ter 1 jogador por vez. Maior peça de engenharia do roadmap (tabela nova, tela de escalação clicável). **Só compensa depois do item 5 existir**: o valor real é o time conseguir anunciar a vaga exata que falta ("procurando Lateral Esquerdo") em vez de "recrutando" genérico.

## Fora deste roadmap por enquanto

Nada — os 6 itens acima cobrem tudo que foi pedido até 2026-09-17. Qualquer ideia nova entra como item 7+ quando surgir.

## Lembrete de sempre

Testar leitura **e** escrita do fluxo real antes de subir pra produção, principalmente o item 3 (dinheiro real envolvido). Nenhuma migração apaga coluna/tabela existente — tudo aditivo, seguindo o padrão já usado em `_garantir_colunas_novas` no `app.py`.
