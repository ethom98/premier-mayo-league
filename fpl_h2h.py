import argparse
import json
import os
import re
from http.server import SimpleHTTPRequestHandler, HTTPServer
from pathlib import Path
from collections import defaultdict

import requests
import yaml

DATA_DIR = Path("data")
DATA_DIR.mkdir(exist_ok=True)

BOOTSTRAP_URL = "https://fantasy.premierleague.com/api/bootstrap-static/"
ENTRY_HISTORY_URL = "https://fantasy.premierleague.com/api/entry/{entry_id}/history/"
PICKS_URL = "https://fantasy.premierleague.com/api/entry/{entry_id}/event/{gw}/picks/"
LIVE_URL = "https://fantasy.premierleague.com/api/event/{gw}/live/"

TOKEN_RE = re.compile(r"^(SEED[1-6]|WINNER_SF[12]|LOSER_SF[12]|WINNER_SHIELD_SF[12])$")

# -------------------------
# Config & schedule loading
# -------------------------

def load_config():
    with open("config.yml", "r") as f:
        cfg = yaml.safe_load(f)
    # label preference: team_name -> name
    id_to_label = {}
    entry_ids = []
    for m in cfg["managers"]:
        label = (m.get("team_name") or m.get("name") or str(m.get("entry_id")))
        eid = int(m["entry_id"])
        id_to_label[eid] = label
        entry_ids.append(eid)
    return cfg, id_to_label, entry_ids

def write_schedule_json(rows, id_to_label):
    out = []
    for r in rows:
        h = r["home"]; a = r["away"]
        def label(x):
            if isinstance(x, int):
                return id_to_label.get(x, str(x))
            return x  # token string
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

# -------------------------
# FPL points helpers
# -------------------------

def get_final_points(entry_id, gw):
    # Official finalized points (after a GW ends)
    url = ENTRY_HISTORY_URL.format(entry_id=entry_id)
    r = requests.get(url, timeout=20)
    r.raise_for_status()
    cur = r.json().get("current", [])
    for item in cur:
        if item.get("event") == gw:
            return item.get("points", 0)
    return 0

def get_live_points(entry_id, gw):
    # Provisional live score for current GW (captaincy via picks multipliers; bench/autosubs best effort)
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

# -------------------------
# Standings support
# -------------------------

def load_standings_sorted():
    """Return standings teams list sorted by points desc if file exists, else []."""
    p = DATA_DIR / "standings.json"
    if not p.exists():
        return []
    data = json.load(open(p))
    return data.get("teams", [])

def update_standings(cfg, schedule, upto_gw, id_to_label):
    table = {
        eid: {"entry_id": eid, "name": id_to_label[eid], "played": 0, "wins": 0, "draws": 0, "losses": 0,
              "points_for": 0, "points_against": 0, "points": 0}
        for eid in id_to_label.keys()
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

# -------------------------
# Token resolving (playoffs)
# -------------------------

def seed_map_from_standings(standings):
    seeds = {}
    for i, team in enumerate(standings, start=1):
        if i > 6: break
        eid = team.get("entry_id")
        if eid:
            seeds[f"SEED{i}"] = eid
    return seeds

def resolve_token(side, standings):
    """Resolve a single token to an entry_id if possible; else return token string."""
    if isinstance(side, int):
        return side
    if not isinstance(side, str) or not TOKEN_RE.match(side):
        return side

    seeds = seed_map_from_standings(standings)

    if side.startswith("SEED"):
        return seeds.get(side, side)

    # Semi ties live in GW31-32; Finals GW33-34; Shield SF GW35-36.
    def collect_tie_legs(gw_list, candidates):
        legs = []
        for g in gw_list:
            p = DATA_DIR / f"gw_{g}_results.json"
            if not p.exists(): 
                continue
            data = json.load(open(p))
            for m in data.get("matches", []):
                s = {m["home_entry_id"], m["away_entry_id"]}
                if all(c in s for c in candidates):
                    legs.append(m)
        return legs

    if side in ("WINNER_SF1","LOSER_SF1"):
        a, b = seeds.get("SEED1"), seeds.get("SEED4")
        if a and b:
            legs = collect_tie_legs([31,32], [a,b])
            if len(legs) >= 2:
                scores = {a:0, b:0}
                for m in legs:
                    scores[m["home_entry_id"]] += m["home_points"]
                    scores[m["away_entry_id"]] += m["away_points"]
                winner = a if scores[a] >= scores[b] else b
                loser  = b if winner == a else a
                return winner if side.startswith("WINNER") else loser
        return side

    if side in ("WINNER_SF2","LOSER_SF2"):
        a, b = seeds.get("SEED2"), seeds.get("SEED3")
        if a and b:
            legs = collect_tie_legs([31,32], [a,b])
            if len(legs) >= 2:
                scores = {a:0, b:0}
                for m in legs:
                    scores[m["home_entry_id"]] += m["home_points"]
                    scores[m["away_entry_id"]] += m["away_points"]
                winner = a if scores[a] >= scores[b] else b
                loser  = b if winner == a else a
                return winner if side.startswith("WINNER") else loser
        return side

    if side.startswith("WINNER_SHIELD_SF"):
        # Read GW35-36 and pick SF1/SF2 winners in file order
        g1 = DATA_DIR / "gw_35_results.json"
        g2 = DATA_DIR / "gw_36_results.json"
        ties = []
        for p in (g1, g2):
            if p.exists():
                d = json.load(open(p))
                for m in d.get("matches", []):
                    key = tuple(sorted([m["home_entry_id"], m["away_entry_id"]], key=lambda x: (isinstance(x, str), x)))
                    if key not in [t[0] for t in ties]:
                        ties.append((key, []))
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
        if len(legs) >= 2 and all(isinstance(x, int) for x in keys[idx]):
            a, b = keys[idx]
            scores = {a:0, b:0}
            for m in legs:
                scores[m["home_entry_id"]] += m["home_points"]
                scores[m["away_entry_id"]] += m["away_points"]
            winner = a if scores[a] >= scores[b] else b
            return winner
        return side

    return side

# -------------------------
# Core compute (per GW)
# -------------------------

def compute_results(cfg, schedule, gw, mode, id_to_label):
    standings = load_standings_sorted()

    # Resolve tokens for this GW
    matches = [m for m in schedule if m["gw"] == gw]
    out = {"gw": gw, "mode": mode, "matches": []}

    for m in matches:
        h = resolve_token(m["home"], standings)
        a = resolve_token(m["away"], standings)
        h_name = id_to_label.get(h, str(h)) if isinstance(h, int) else str(h)
        a_name = id_to_label.get(a, str(a)) if isinstance(a, int) else str(a)

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

# -------------------------
# Prizes: load & calculators
# -------------------------

def load_prizes():
    path = Path("prizes.yml")
    if not path.exists():
        return None
    with open(path, "r") as f:
        return yaml.safe_load(f)

def get_all_entry_points_for_gw(entry_ids, gw, mode):
    pts = {}
    for eid in entry_ids:
        try:
            pts[eid] = resolve_points(eid, gw, mode)
        except Exception:
            pts[eid] = 0
    return pts

def calc_weekly_winners(entry_ids, upto_gw, mode, per_gw_amount):
    """
    Returns:
      weekly: [ {gw, winners: [{entry_id, points, amount}], pot_per_gw} ... ]
      totals_by_entry: { entry_id: total_amount }
    """
    weekly = []
    totals = defaultdict(float)

    for gw in range(1, upto_gw + 1):
        # Need valid results: rely on direct points calculation (doesn't depend on schedule)
        pts = get_all_entry_points_for_gw(entry_ids, gw, mode)
        max_pts = max(pts.values()) if pts else 0
        winners = [eid for eid, p in pts.items() if p == max_pts]
        amount_each = round(per_gw_amount / max(1, len(winners)), 2) if per_gw_amount else 0.0

        week_row = {
            "gw": gw,
            "pot_per_gw": per_gw_amount,
            "winners": [{"entry_id": eid, "points": pts[eid], "amount": amount_each} for eid in winners]
        }
        weekly.append(week_row)
        for eid in winners:
            totals[eid] += amount_each

    return weekly, {eid: round(amt, 2) for eid, amt in totals.items()}

def calc_block_points(entry_ids, gw_start, gw_end, mode):
    block_totals = defaultdict(int)
    best_single = defaultdict(int)
    for gw in range(gw_start, gw_end + 1):
        pts = get_all_entry_points_for_gw(entry_ids, gw, mode)
        for eid, p in pts.items():
            block_totals[eid] += p
            if p > best_single[eid]:
                best_single[eid] = p
    return block_totals, best_single

def compute_mystery_kits(entry_ids, prizes_cfg, upto_gw, mode):
    """
    Reads blocks from prizes.yml and computes leader/winner per block.
    Returns dict suitable for data/mystery_kits.json
    """
    mk = prizes_cfg["allocations"]["mystery_kits"]
    blocks = mk.get("blocks", [])
    result_blocks = []

    for idx, b in enumerate(blocks, start=1):
        s, e = int(b["gw_start"]), int(b["gw_end"])
        # status
        status = "in_progress" if upto_gw < e else "complete"
        effective_end = min(upto_gw, e)

        block_totals, best_single = calc_block_points(entry_ids, s, effective_end, mode)
        if not block_totals:
            leaders = []
        else:
            top_total = max(block_totals.values())
            leaders = [eid for eid, tot in block_totals.items() if tot == top_total]

        winner_eids = []
        tiebreak = None
        note = ""

        if status == "complete" and leaders:
            if len(leaders) == 1:
                winner_eids = leaders
            else:
                # tiebreaker 1: highest single-GW score in block
                max_single = max(best_single[eid] for eid in leaders)
                tb_leaders = [eid for eid in leaders if best_single[eid] == max_single]
                if len(tb_leaders) == 1:
                    winner_eids = tb_leaders
                    tiebreak = "highest_single_gw_score_in_block"
                else:
                    # coin flip requested; we cannot flip deterministically here, so award all tied and note it.
                    winner_eids = tb_leaders
                    tiebreak = "coin_flip_required"
                    note = "Multiple winners tied after tiebreaker; resolve via coin flip offline."

        result_blocks.append({
            "index": idx,
            "name": b.get("name", f"Block {idx}"),
            "gw_start": s, "gw_end": e,
            "status": status,
            "leaders": [{"entry_id": eid, "total_points": block_totals.get(eid, 0), "best_single_gw": best_single.get(eid, 0)} for eid in sorted(block_totals.keys())],
            "current_top_total": max(block_totals.values()) if block_totals else 0,
            "winners": winner_eids,
            "tiebreak_used": tiebreak,
            "note": note
        })

    return {"blocks": result_blocks}

# -------------------------
# Winnings & kits writers
# -------------------------

def write_winnings_and_kits(cfg, id_to_label, entry_ids, upto_gw, mode):
    prizes = load_prizes()
    if not prizes:
        return  # nothing to do

    currency = (prizes.get("display") or {}).get("currency", "USD")
    per_gw = prizes["allocations"]["weekly_top_scorer"]["per_gw_amount"]

    weekly_rows, totals_by_entry = calc_weekly_winners(entry_ids, upto_gw, mode, per_gw)

    # Mystery kits
    mk = compute_mystery_kits(entry_ids, prizes, upto_gw, mode)

    # Build a per-team winnings/kit summary
    summary = []
    kits_won_count = defaultdict(int)
    for b in mk["blocks"]:
        for eid in b.get("winners", []):
            kits_won_count[eid] += 1

    for eid in entry_ids:
        summary.append({
            "entry_id": eid,
            "name": id_to_label[eid],
            "weekly_winnings": round(totals_by_entry.get(eid, 0.0), 2),
            "kits_won": kits_won_count.get(eid, 0)
        })

    winnings_payload = {
        "currency": currency,
        "weekly": weekly_rows,
        "totals": summary
    }
    with open(DATA_DIR / "winnings.json", "w") as f:
        json.dump(winnings_payload, f, indent=2)

    with open(DATA_DIR / "mystery_kits.json", "w") as f:
        json.dump(mk, f, indent=2)

# -------------------------
# Tiny static server (optional)
# -------------------------

def serve():
    class Handler(SimpleHTTPRequestHandler):
        pass
    port = 8765
    httpd = HTTPServer(("0.0.0.0", port), Handler)
    print(f"Serving at http://127.0.0.1:{port}")
    httpd.serve_forever()

# -------------------------
# Main
# -------------------------

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--gw", type=int, default=1, help="Gameweek number to process")
    parser.add_argument("--mode", choices=["live","final"], default="final", help="Use live or final points")
    parser.add_argument("--serve", action="store_true", help="Serve a tiny website at http://127.0.0.1:8765")
    args = parser.parse_args()

    cfg, id_to_label, entry_ids = load_config()
    schedule = load_schedule()
    write_schedule_json(schedule, id_to_label)

    if args.serve:
        serve()
        return

    # Compute this GW and update standings up to this GW
    compute_results(cfg, schedule, args.gw, args.mode, id_to_label)
    update_standings(cfg, schedule, args.gw, id_to_label)

    # NEW: write winnings + mystery kits
    write_winnings_and_kits(cfg, id_to_label, entry_ids, args.gw, args.mode)

    print(f"Wrote data/gw_{args.gw}_results.json, data/standings.json, data/winnings.json, data/mystery_kits.json")

if __name__ == "__main__":
    main()
