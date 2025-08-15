import argparse
import json
import os
import re
from http.server import SimpleHTTPRequestHandler, HTTPServer
from pathlib import Path

import requests
import yaml

DATA_DIR = Path("data")
DATA_DIR.mkdir(exist_ok=True)

BOOTSTRAP_URL = "https://fantasy.premierleague.com/api/bootstrap-static/"
ENTRY_HISTORY_URL = "https://fantasy.premierleague.com/api/entry/{entry_id}/history/"
PICKS_URL = "https://fantasy.premierleague.com/api/entry/{entry_id}/event/{gw}/picks/"
LIVE_URL = "https://fantasy.premierleague.com/api/event/{gw}/live/"

TOKEN_RE = re.compile(r"^(SEED[1-6]|WINNER_SF[12]|LOSER_SF[12]|WINNER_SHIELD_SF[12])$")

def load_config():
    with open("config.yml", "r") as f:
        cfg = yaml.safe_load(f)
    id_to_name = {m["entry_id"]: m.get("team_name") or m["name"] for m in cfg["managers"]}
    return cfg, id_to_name

def write_schedule_json(rows, id_to_name):
    out = []
    for r in rows:
        h = r["home"]; a = r["away"]
        def label(x):
            if isinstance(x, int):
                return id_to_name.get(x, str(x))
            return x
        out.append({"gw": r["gw"], "home_name": label(h), "away_name": label(a)})
    with open(DATA_DIR / "schedule.json", "w") as f:
        json.dump({"matches": out}, f, indent=2)

def load_schedule():
    """Loads schedule.csv which may contain Entry IDs (ints) or playoff tokens (strings)."""
    rows_raw = []
    with open("schedule.csv", "r", newline="") as f:
        for i, line in enumerate(f):
            if i == 0 and line.lower().startswith("gw,"):
                continue
            parts = [p.strip() for p in line.strip().split(",")]
            if len(parts) != 3: 
                continue
            gw, home, away = parts
            def parse_side(s):
                try:
                    return int(s)
                except:
                    return s  # token string
            rows_raw.append({"gw": int(gw), "home": parse_side(home), "away": parse_side(away)})
    return rows_raw

def get_final_points(entry_id, gw):
    url = ENTRY_HISTORY_URL.format(entry_id=entry_id)
    r = requests.get(url, timeout=20)
    r.raise_for_status()
    cur = r.json().get("current", [])
    for item in cur:
        if item.get("event") == gw:
            return item.get("points", 0)
    return 0

def get_live_points(entry_id, gw):
    picks_url = PICKS_URL.format(entry_id=entry_id, gw=gw)
    pr = requests.get(picks_url, timeout=20)
    pr.raise_for_status()
    picks = pr.json().get("picks", [])
    elements = {p["element"]: p.get("multiplier", 0) for p in picks if p.get("multiplier", 0) > 0}

    live_url = LIVE_URL.format(gw=gw)
    lr = requests.get(live_url, timeout=20)
    lr.raise_for_status()
    live = lr.json().get("elements", [])
    live_points = 0
    for el in live:
        el_id = el.get("id")
        if el_id in elements:
            pts = el.get("stats", {}).get("total_points", 0)
            live_points += pts * elements[el_id]
    return live_points

def resolve_points(entry_id, gw, mode="final"):
    return get_final_points(entry_id, gw) if mode == "final" else get_live_points(entry_id, gw)

def load_standings_sorted():
    """Load standings.json and return list sorted by points desc (same as we write it)."""
    p = DATA_DIR / "standings.json"
    if not p.exists():
        return []
    data = json.load(open(p))
    return data.get("teams", [])

def seed_map_from_standings(standings):
    """Return dict like {'SEED1': entry_id, ...}."""
    seeds = {}
    for i, team in enumerate(standings, start=1):
        if i > 6: break
        eid = team.get("entry_id")
        if eid:
            seeds[f"SEED{i}"] = eid
    return seeds

def aggregate_tie(gw_files):
    """Given list of GW result files (two legs), return (winner_id, loser_id) by aggregate points."""
    from collections import defaultdict
    scores = defaultdict(int)
    teams = set()
    for path in gw_files:
        if not path.exists():
            continue
        data = json.load(open(path))
        for m in data.get("matches", []):
            if isinstance(m["home_entry_id"], int) and isinstance(m["away_entry_id"], int):
                scores[m["home_entry_id"]] += m["home_points"]
                scores[m["away_entry_id"]] += m["away_points"]
                teams.add(m["home_entry_id"]); teams.add(m["away_entry_id"])
    if len(teams) != 2:
        return None, None
    items = list(scores.items())
    items.sort(key=lambda kv: kv[1], reverse=True)
    if len(items) < 2:
        return None, None
    return items[0][0], items[1][0]

def resolve_token(side, current_gw, standings):
    """Resolve a single token to an entry_id if possible, else return the token string."""
    if isinstance(side, int):
        return side
    if not isinstance(side, str) or not TOKEN_RE.match(side):
        return side

    seeds = seed_map_from_standings(standings)

    if side.startswith("SEED"):
        return seeds.get(side, side)

    # Semis are GW31-32; Finals are GW33-34; Shield SFs are GW35-36
    if side in ("WINNER_SF1","LOSER_SF1"):
        # SF1 is SEED1 vs SEED4; look up those IDs from standings, then aggregate GW31+32 for that tie
        a = seeds.get("SEED1"); b = seeds.get("SEED4")
        if a and b:
            g1 = DATA_DIR / "gw_31_results.json"
            g2 = DATA_DIR / "gw_32_results.json"
            # Filter legs to only the ones matching this tie
            def tie_filter(path):
                if not path.exists(): return []
                data = json.load(open(path))
                legs = []
                for m in data.get("matches", []):
                    s = {m["home_entry_id"], m["away_entry_id"]}
                    if a in s and b in s:
                        legs.append(m)
                return legs
            legs = tie_filter(g1) + tie_filter(g2)
            if len(legs) >= 2:
                # aggregate
                scores = {a:0,b:0}
                for m in legs:
                    scores[m["home_entry_id"]] += m["home_points"]
                    scores[m["away_entry_id"]] += m["away_points"]
                winner = a if scores[a] >= scores[b] else b
                loser  = b if winner == a else a
                return winner if side.startswith("WINNER") else loser
        return side

    if side in ("WINNER_SF2","LOSER_SF2"):
        a = seeds.get("SEED2"); b = seeds.get("SEED3")
        if a and b:
            g1 = DATA_DIR / "gw_31_results.json"
            g2 = DATA_DIR / "gw_32_results.json"
            def tie_filter(path):
                if not path.exists(): return []
                data = json.load(open(path))
                legs = []
                for m in data.get("matches", []):
                    s = {m["home_entry_id"], m["away_entry_id"]}
                    if a in s and b in s:
                        legs.append(m)
                return legs
            legs = tie_filter(g1) + tie_filter(g2)
            if len(legs) >= 2:
                scores = {a:0,b:0}
                for m in legs:
                    scores[m["home_entry_id"]] += m["home_points"]
                    scores[m["away_entry_id"]] += m["away_points"]
                winner = a if scores[a] >= scores[b] else b
                loser  = b if winner == a else a
                return winner if side.startswith("WINNER") else loser
        return side

    if side.startswith("WINNER_SHIELD_SF"):
        # Shield SF1: SEED5 vs SEED6; Shield SF2: LOSER_SF1 vs LOSER_SF2
        # We'll aggregate GW35-36 results
        g1 = DATA_DIR / "gw_35_results.json"
        g2 = DATA_DIR / "gw_36_results.json"
        # Determine which tie this token refers to by reading GW35 legs in order
        # We'll collect ties and pick the first as SF1, second as SF2
        ties = []
        for p in (g1, g2):
            if p.exists():
                d = json.load(open(p))
                for m in d.get("matches", []):
                    key = tuple(sorted([m["home_entry_id"], m["away_entry_id"]], key=lambda x: (isinstance(x, str), x)))
                    if key not in [t[0] for t in ties]:
                        ties.append((key, []))
        # build legs dict
        legs_by_key = {key: [] for key,_ in ties}
        for p in (g1, g2):
            if p.exists():
                d = json.load(open(p))
                for m in d.get("matches", []):
                    key = tuple(sorted([m["home_entry_id"], m["away_entry_id"]], key=lambda x: (isinstance(x, str), x)))
                    if key in legs_by_key:
                        legs_by_key[key].append(m)

        keys = list(legs_by_key.keys())
        if len(keys) < 2:
            return side
        idx = 0 if side.endswith("SF1") else 1
        legs = legs_by_key.get(keys[idx], [])
        if len(legs) >= 2 and all(isinstance(x, int) for x in [legs[0]["home_entry_id"], legs[0]["away_entry_id"]]):
            # aggregate winner
            a, b = keys[idx]
            scores = {a:0,b:0}
            for m in legs:
                scores[m["home_entry_id"]] += m["home_points"]
                scores[m["away_entry_id"]] += m["away_points"]
            winner = a if scores[a] >= scores[b] else b
            return winner
        return side

    return side

def compute_results(cfg, schedule, gw, mode):
    id_to_name = {m["entry_id"]: (m.get("team_name") or m["name"]) for m in cfg["managers"]}
    standings = load_standings_sorted()

    # Resolve tokens for this GW
    matches = [m for m in schedule if m["gw"] == gw]
    out = {"gw": gw, "mode": mode, "matches": []}

    for m in matches:
        h = resolve_token(m["home"], gw, standings)
        a = resolve_token(m["away"], gw, standings)
        h_name = id_to_name.get(h, str(h)) if isinstance(h, int) else str(h)
        a_name = id_to_name.get(a, str(a)) if isinstance(a, int) else str(a)

        # If unresolved token remains, mark pending
        if not isinstance(h, int) or not isinstance(a, int):
            out["matches"].append({
                "home_entry_id": h, "home_name": h_name,
                "home_points": 0,
                "away_entry_id": a, "away_name": a_name,
                "away_points": 0,
                "status": "pending (seed/winner not resolved yet)"
            })
            continue

        try:
            hp = resolve_points(h, gw, mode)
            ap = resolve_points(a, gw, mode)
            status = "final" if mode == "final" else "live"
        except Exception as e:
            hp = ap = 0
            status = f"pending ({e})"

        out["matches"].append({
            "home_entry_id": h, "home_name": h_name,
            "home_points": hp,
            "away_entry_id": a, "away_name": a_name,
            "away_points": ap,
            "status": status
        })

    # Save GW results
    with open(DATA_DIR / f"gw_{gw}_results.json", "w") as f:
        json.dump(out, f, indent=2)

    return out

def update_standings(cfg, schedule, upto_gw):
    id_to_name = {m["entry_id"]: (m.get("team_name") or m["name"]) for m in cfg["managers"]}
    table = {
        eid: {"entry_id": eid, "name": id_to_name[eid], "played": 0, "wins": 0, "draws": 0, "losses": 0,
              "points_for": 0, "points_against": 0, "points": 0}
        for eid in id_to_name.keys()
    }

    for gw in range(1, upto_gw + 1):
        path = DATA_DIR / f"gw_{gw}_results.json"
        if not path.exists():
            continue
        results = json.load(open(path))
        for m in results.get("matches", []):
            if not (isinstance(m["home_entry_id"], int) and isinstance(m["away_entry_id"], int)):
                continue  # skip pending token matches
            h_id, a_id = m["home_entry_id"], m["away_entry_id"]
            hp, ap = m["home_points"], m["away_points"]

            table[h_id]["points_for"] += hp
            table[h_id]["points_against"] += ap
            table[a_id]["points_for"] += ap
            table[a_id]["points_against"] += hp

            table[h_id]["played"] += 1
            table[a_id]["played"] += 1
            if hp > ap:
                table[h_id]["wins"] += 1
                table[a_id]["losses"] += 1
                table[h_id]["points"] += 3
            elif ap > hp:
                table[a_id]["wins"] += 1
                table[h_id]["losses"] += 1
                table[a_id]["points"] += 3
            else:
                table[h_id]["draws"] += 1
                table[a_id]["draws"] += 1
                table[h_id]["points"] += 1
                table[a_id]["points"] += 1

    teams = list(table.values())
    teams.sort(key=lambda t: (t["points"], t["points_for"] - t["points_against"], t["points_for"]), reverse=True)
    with open(DATA_DIR / "standings.json", "w") as f:
        json.dump({"teams": teams}, f, indent=2)
    return {"teams": teams}

def serve():
    class Handler(SimpleHTTPRequestHandler):
        pass
    port = 8765
    httpd = HTTPServer(("0.0.0.0", port), Handler)
    print(f"Serving at http://127.0.0.1:{port}")
    httpd.serve_forever()

def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--gw", type=int, default=1, help="Gameweek number to process")
    parser.add_argument("--mode", choices=["live","final"], default="final", help="Use live or final points")
    parser.add_argument("--serve", action="store_true", help="Serve a tiny website at http://127.0.0.1:8765")
    args = parser.parse_args()

    cfg, id_to_name = load_config()
    schedule = load_schedule()
    write_schedule_json(schedule, id_to_name)

    if args.serve:
        serve()
        return

    compute_results(cfg, schedule, args.gw, args.mode)
    update_standings(cfg, schedule, args.gw)
    print(f"Wrote data/gw_{args.gw}_results.json and data/standings.json")

if __name__ == "__main__":
    main()
