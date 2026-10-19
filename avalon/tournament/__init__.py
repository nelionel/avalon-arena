"""Tournament mode: teams enter a prompt, and the arena plays it with a fixed model.

  teams.py    submissions: teams/<team>/player.md (+ optional team.toml), checks and caps
  player.py   the prompt player: one stateless request per turn, a memory file, the fault policy
  series.py   balanced role rotation, the tournament runner, standings
  publish.py  public bundles (no secrets before their time), the static site, trajectories

See TOURNAMENT.md.
"""
