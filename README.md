# Premier Mayo League — Custom FPL H2H Dashboard

Live/Final scoring pulled from FPL public endpoints, with a **custom 6‑team schedule** (GW1–30) and a **Top‑4 Championship + Shield** playoff bracket (GW31–38).

## Quick Start (local)
```bash
pip install -r requirements.txt
python fpl_h2h.py --gw 1 --mode live   # during a GW
python fpl_h2h.py --gw 1 --mode final  # after a GW ends
python fpl_h2h.py --serve              # optional: tiny local site at http://127.0.0.1:8765
```

Edit **config.yml** to update names/IDs. The season schedule lives in **schedule.csv**.  
The script writes JSON into **/data** for the site (`gw_<GW>_results.json`, `standings.json`, `schedule.json`).

## Playoffs (Top‑4 + Shield)
- GW31–32: Semifinals — **SEED1 vs SEED4**, **SEED2 vs SEED3** (two legs)
- GW33–34: Final — **WINNER_SF1 vs WINNER_SF2** (two legs)
- GW35–36: Shield Semis — **SEED5 vs SEED6**, **LOSER_SF1 vs LOSER_SF2**
- GW37–38: Shield Final — **WINNER_SHIELD_SF1 vs WINNER_SHIELD_SF2**

**Ties:** aggregate points over both legs; tie → higher regular‑season seed advances.

## Deploy on GitHub Pages (Actions)
1. Push this repo to GitHub.  
2. Settings → Pages → Source = **GitHub Actions**.  
3. The included workflow runs every 30 minutes and publishes `index.html` + `data/`.

You can also add a weekly `--mode final` job after the GW ends to lock official totals.
