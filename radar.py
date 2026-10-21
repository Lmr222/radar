#!/usr/bin/env python3
"""Radar de gros paris — Polymarket (données publiques, gratuites, sans clé).

Repère sur les marchés sportifs :
  🐋 un gros pari isolé, ou un afflux d'argent sur une même option
  📈 un pic de volume sur un match
  ⚡ une forte variation de prix
puis envoie une alerte Telegram. Le script ne parie JAMAIS : la décision t'appartient.

Usage local :  python radar.py --dry   (affiche les alertes, n'envoie rien)
"""
import json
import os
import statistics
import sys
import time
from pathlib import Path

import requests


def env(name, default):
    return os.getenv(name) or default  # vide ou absent -> valeur par défaut


GAMMA, DATA = "https://gamma-api.polymarket.com", "https://data-api.polymarket.com"
STATE_FILE = Path(env("STATE_FILE", "state/state.json"))
TAGS = [t.strip() for t in env("SPORT_TAGS", "soccer,basketball,nba,tennis,epl,champions-league").split(",") if t.strip()]
MAX_EVENTS = int(env("MAX_EVENTS", "40"))          # nb de matchs surveillés (les plus actifs)
MIN_TRADE = float(env("MIN_TRADE_USD", "500"))     # on ignore les paris plus petits
WHALE = float(env("WHALE_USD", "1500"))            # un seul pari >= ce montant -> alerte
CLUSTER = float(env("CLUSTER_USD", "4000"))        # total sur UNE option depuis le dernier passage
PRICE_MOVE = float(env("PRICE_MOVE", "0.08"))      # variation de prix (0.08 = 8 points)
SPIKE_X = float(env("SPIKE_FACTOR", "4"))          # volume >= 4x la normale
SPIKE_MIN = float(env("SPIKE_MIN_USD", "10000"))   # ... et au moins ce montant
TOKEN, CHAT = os.getenv("TELEGRAM_TOKEN"), os.getenv("TELEGRAM_CHAT_ID")
DRY = "--dry" in sys.argv


def get(url, **params):
    r = requests.get(url, params=params, timeout=30, headers={"User-Agent": "bet-radar/1.0"})
    r.raise_for_status()
    return r.json()


def jl(x):
    """Gamma renvoie parfois les listes sous forme de texte JSON."""
    return json.loads(x) if isinstance(x, str) else (x or [])


def notify(text):
    print(text, "\n---", flush=True)
    if DRY or not (TOKEN and CHAT):
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TOKEN}/sendMessage",
            json={"chat_id": CHAT, "text": text, "disable_web_page_preview": True},
            timeout=30,
        ).raise_for_status()
    except Exception as e:
        print(f"[warn] Telegram: {e}", flush=True)


def load_state():
    try:
        return json.loads(STATE_FILE.read_text())
    except Exception:
        return {}


def save_state(state):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state))


def fetch_events():
    events, counts = {}, {}
    for tag in TAGS:
        try:
            data = get(f"{GAMMA}/events", tag_slug=tag, active="true", closed="false", limit=100)
        except Exception as e:
            print(f"[warn] tag {tag}: {e}", flush=True)
            data = []
        counts[tag] = len(data)
        for ev in data:
            events[ev["id"]] = ev
    top = sorted(events.values(), key=lambda e: float(e.get("volume24hr") or 0), reverse=True)
    return top[:MAX_EVENTS], counts


def scan_events(events, prev):
    """Pics de volume et variations de prix, par comparaison avec le passage précédent."""
    alerts, new = [], {}
    for ev in events:
        eid, p = str(ev["id"]), prev.get(str(ev["id"]), {})
        title = ev.get("title", "?")
        link = f"https://polymarket.com/event/{ev.get('slug', '')}"
        vol, deltas = float(ev.get("volume") or 0), list(p.get("deltas", []))
        if "volume" in p:
            d = vol - p["volume"]
            if len(deltas) >= 3:
                normal = statistics.median(deltas)
                if d >= SPIKE_MIN and d >= SPIKE_X * max(normal, 1):
                    alerts.append(f"📈 Pic de volume\n{title}\n+{d:,.0f} $ depuis le dernier passage (normal : ~{normal:,.0f} $)\n{link}")
            deltas = (deltas + [d])[-30:]
        prices = {}
        for m in ev.get("markets") or []:
            for o, x in zip(jl(m.get("outcomes")), jl(m.get("outcomePrices"))):
                k, x = f"{m.get('conditionId')}|{o}", float(x)
                prices[k] = x
                old = p.get("prices", {}).get(k)
                if old is not None and x - old >= PRICE_MOVE:  # hausse = l'argent arrive sur cette option
                    alerts.append(f"⚡ Forte variation de prix\n{title}\n{m.get('question', '')} → {o} : {old*100:.0f} % → {x*100:.0f} %\n{link}")
        new[eid] = {"volume": vol, "deltas": deltas, "prices": prices}
    return alerts, new


def scan_trades(events, seen, first):
    """Gros paris : on regroupe les nouveaux achats par option."""
    ids = ",".join(str(e["id"]) for e in events)
    trades = get(f"{DATA}/trades", eventId=ids, filterType="CASH", filterAmount=MIN_TRADE, limit=500)
    groups = {}
    for t in trades:
        key = f"{t.get('transactionHash')}:{t.get('asset')}:{t.get('size')}"
        if key in seen:
            continue
        seen[key] = int(time.time())
        if first or t.get("side") != "BUY":  # 1er passage = historique, on n'alerte pas
            continue
        cash = float(t["size"]) * float(t["price"])
        g = groups.setdefault((t["conditionId"], t["outcome"]), dict(
            title=t.get("title", "?"), outcome=t["outcome"], slug=t.get("eventSlug", ""),
            total=0.0, n=0, biggest=0.0, price=float(t["price"])))
        g["total"] += cash
        g["n"] += 1
        g["biggest"] = max(g["biggest"], cash)
    alerts = []
    for g in groups.values():
        if g["biggest"] >= WHALE or g["total"] >= CLUSTER:
            odds = f" (cote ≈ {1/g['price']:.2f})" if g["price"] > 0 else ""
            alerts.append(
                f"🐋 Gros paris détectés\n{g['title']}\nOption : {g['outcome']}\n"
                f"{g['n']} paris, total ≈ {g['total']:,.0f} $ (le plus gros : {g['biggest']:,.0f} $)\n"
                f"Prix actuel : {g['price']*100:.0f} %{odds}\nhttps://polymarket.com/event/{g['slug']}")
    return alerts


def main():
    state = load_state()
    first = not state
    events, counts = fetch_events()
    seen, alerts, new_events = state.get("seen", {}), [], {}
    if events:
        a1, new_events = scan_events(events, state.get("events", {}))
        try:
            a2 = scan_trades(events, seen, first)
        except Exception as e:
            print(f"[warn] trades: {e}", flush=True)
            a2 = []
        alerts = [] if first else a1 + a2
    save_state({"events": new_events, "seen": dict(list(seen.items())[-3000:]), "ts": int(time.time())})
    if first:
        notify(f"✅ Radar démarré. {len(events)} matchs surveillés.\nÉvénements trouvés par tag : {counts}")
    for a in alerts[:10]:
        notify(a)
    print(f"OK — {len(events)} matchs, {len(alerts)} alerte(s)", flush=True)


if __name__ == "__main__":
    main()
