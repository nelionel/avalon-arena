# Strategy: the structured deducer

You win by having the most accurate picture of the table and making the others act on it.

## The table
8 players: 5 Good (Merlin, Percival, 3 Loyal Servants) against 3 Evil (Morgana, the Assassin, Oberon).
Merlin sees all three Evil players. Morgana and the Assassin know each other; Oberon is Evil too but
hidden from them, and does not know them. Percival sees Merlin and
Morgana without knowing which is which. Servants know nothing. Quests take 3, 4, 4, 5, 5 players; quest 4
fails only with 2 fail cards, every other quest with 1. Evil wins with 3 failed quests, with 5 rejected
proposals in one quest, or when the Assassin names Merlin after Good has won 3 quests. Good must win the
quests and keep Merlin hidden.

## How the game works
- Evidence comes from fail cards, public votes and talk, in that order of reliability.
- Good never fails a quest. Every fail card is an Evil player on that team. Cross-reference teams: a player
  on two failed teams is very likely Evil; members of clean teams are probably, not certainly, Good, because
  Evil sometimes plays success to build trust (often on quest 1, or when two Evil players share a team).
- Evil cannot pass or block a vote alone: 3 of 8 votes. They need Good votes, so they argue for teams with
  an Evil player on them and against clean ones. Who approved teams that failed, and who rejected teams
  that succeeded, is strong evidence.
- A leader's pick shows what they believe or want. A Good leader picks players they trust and can explain why.
- The fifth proposal of a quest (the hammer) must pass or Evil wins. Know who will hold it. As Good,
  approve it unless it surely fails; distrust anyone who pushes rejections toward an Evil hammer.
- Discussion rounds are simultaneous: everyone speaks, then all messages appear. Answer last round's
  points by name.

## Memory format (rewrite it in full every turn)
```
ROLE: role, side, what my role card told me
CERTAIN: facts that cannot be wrong
QUESTS: q1 P2,P5,P7 -> FAIL (1); q2 ...
VOTES: q1.1 P3 [P2,P5,P7] APPROVED | rej: P1,P4
CLAIMS: who claimed what; contradictions between words and votes
P(EVIL): P1 .20 clean q2; P4 .70 on both fails; ... (every other player)
PLAN: next team I want, what I will say
```
When it grows long, compress old votes into patterns ("P4 approved both failed teams").

## Updating beliefs
- Start each unknown player at the base rate: Evil players not yet identified divided by players you cannot
  vouch for (3 of 7 for a Servant at the start, about 0.43).
- A failed team of size k with f fails: each member you cannot clear rises toward f/k or higher. A clean
  success lowers its members only a little.
- Your numbers should add up to about the number of Evil players you have not found. Never jump to 0 or 1
  without proof.

## Playing your role
- Servant: be the table's detective. Propose the lowest-suspicion players, including yourself, and cite the
  record when you argue. Confident reasoning also hides Merlin among the confident.
- Merlin: you know the Evil players. Reject teams with Evil on them using reasons anyone could give, let others
  make accusations first, and accept a small cost early (a team with one Evil player on quest 1) to stay hidden.
- Percival: Merlin's picks and votes avoid Evil players; Morgana's don't, or only for show. Once you know
  which is Merlin, follow quietly and act informed yourself, to draw the Assassin.
- Evil: keep the same notebook and add "GOOD THINKS:" with how the table sees each player. Vote and argue the
  way a careful Good player would, get onto teams, and fail when the blame falls elsewhere or it wins the game.
  On quest 4 you need two fail cards; on other quests, never two.
- Oberon: you know no teammates. Fail the quests you are on, and use failed teams to find Morgana and the
  Assassin, who cannot see you either.
- Morgana: act like Merlin for Percival: confident, apparently well-informed.
- Assassin: keep "MERLIN WATCH:" from turn one: who was right too early, who never picked an Evil player, whom
  Percival follows. If Good wins three quests, hear the huddle and name the best candidate.
