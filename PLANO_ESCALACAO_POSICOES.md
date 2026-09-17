# Plano — Escalação: cada jogador numa posição do esquema tático

Status: **planejado, nada implementado ainda**. Escrito em 2026-09-17 pra ser executado depois, possivelmente por outra sessão/modelo — por isso está detalhado o suficiente pra não depender da conversa que o gerou. **Recomendação: essa é a última peça do roadmap** — ver justificativa na seção 7.

## 1. O que existe hoje

`Team.formacao` já existe (ex.: "4-4-2"), escolhido pelo capitão na rota `/time/formacao`. Hoje é só texto decorativo, sem ligação com jogador nenhum — não sabe quem joga em qual posição.

## 2. A ideia (duas coisas separadas, não confundir)

- **Posição preferida** (perfil do jogador, `User`): campo livre, qualquer jogador declara a posição que prefere (ex.: Atacante), independente de estar num time ou não. Serve de triagem/descoberta mesmo antes de entrar num time. Vários jogadores podem preferir a mesma posição sem problema nenhum — isso é só um gosto pessoal declarado.
- **Escalação titular** (por time): os slots do esquema tático escolhido (`Team.formacao`) — por exemplo, "4-4-2" vira 1 Goleiro + 4 Defesa + 4 Meio + 2 Ataque, 11 slots fixos. **Regra pedida pelo Marcelo**: um slot só pode ter **1 jogador por vez** — dois jogadores não podem ocupar o mesmo slot simultaneamente. Fora dos 11 titulares, o resto do elenco fica como reserva, sem problema de ter vários jogadores "sobrando" na mesma posição preferida.

## 3. Quem escala

Autoatribuição: cada jogador clica num slot vago do esquema atual do time e ocupa ele (tipo reservar um assento). Pra trocar de posição, libera o slot atual antes de ocupar outro. O capitão pode remover qualquer jogador de um slot (organização do time) e é o único que muda o esquema tático do time — trocar de esquema (ex.: de "4-4-2" pra "4-3-3") muda a lista de slots; quem estava num slot que deixou de existir vira reserva automaticamente, sem perder o vínculo com o time.

## 4. Como os slots nascem do esquema

Cada formação vira uma lista fixa de posições, mantida num dicionário simples no código, por exemplo:

```
FORMACOES = {
    "4-4-2": ["GOL", "ZAG", "ZAG", "LD", "LE", "VOL", "VOL", "MEI", "MEI", "PD", "ATA", "ATA"],
    "4-3-3": [...],
    ...
}
```

**Confirmar com o Marcelo a nomenclatura exata de posições e quantos slots cada esquema tem antes de implementar** — cada formação de Pro Clubs tem uma leitura ligeiramente diferente (ex.: se "4-4-2" tem 2 volantes fixos ou 2 meias mais um volante) e é melhor fechar isso com ele do que assumir.

## 5. Modelo de dados (tudo aditivo)

- `User.posicao_preferida` (String nullable) — coluna nova
- Tabela nova `EscalacaoSlot`: `id`, `time_id` (FK Team), `posicao` (String, ex.: "ZAG-1"), `usuario_id` (FK User, nullable)
- Ao criar o time ou trocar `Team.formacao`, os registros de `EscalacaoSlot` desse time são recriados a partir do dicionário de formações

## 6. Ligação direta com o mercado de transferências

Quando um time estiver marcado como "recrutando" (mercado de transferências, item separado do roadmap), a aba de Times mostra **quais posições especificamente estão vagas** na escalação (ex.: "procurando: Lateral Esquerdo, Atacante") em vez de só "recrutando" genérico sem detalhe nenhum — muito mais útil tanto pra quem procura time quanto pra quem procura jogador.

## 7. Por que deixar por último

O valor real dessa peça depende do mercado de transferências já existir — sem ele, "escalar posição" vira só um enfeite visual na página do time, sem conectar ninguém a ninguém. Construída depois do mercado de transferências (e do sistema de XP/nível, que mostra o nível do jogador na hora de recrutar), essa peça vira a cereja do bolo: o time mostra exatamente a vaga que falta, com o nível de quem está se candidatando. Construída antes, é trabalho gasto sem efeito prático imediato.

Também é, das peças desse roadmap, a de maior esforço de engenharia: tabela nova, tela de escalação tipo "campinho" clicável, e várias regras de reorganização quando o esquema tático muda no meio do caminho.
