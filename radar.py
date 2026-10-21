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
import re
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
SPORTS = {"soccer": "⚽ FOOTBALL", "epl": "⚽ FOOTBALL", "champions-league": "⚽ FOOTBALL",
          "basketball": "🏀 BASKET", "nba": "🏀 BASKET", "tennis": "🎾 TENNIS"}
# paris sur un joueur précis, ex. « Naz Reid: Points O/U 17.5 » (trop de bruit, ignorés)
PROPS = re.compile(r"^(?!.*\bvs\b)[^:]+:.*O/U", re.I)


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
            ev.setdefault("_sport", SPORTS.get(tag, tag.upper()))
            events.setdefault(ev["id"], ev)
    top = sorted(events.values(), key=lambda e: float(e.get("volume24hr") or 0), reverse=True)
    return top[:MAX_EVENTS], counts


def scan_events(events, prev):
    """Pics de volume et variations de prix, par comparaison avec le passage précédent."""
    alerts, new = [], {}
    for ev in events:
        eid, p = str(ev["id"]), prev.get(str(ev["id"]), {})
        title = ev.get("title", "?")
        link = f"https://polymarket.com/event/{ev.get('slug', '')}"
        head = f"{ev.get('_sport', '')} · {title}"
        vol, deltas = float(ev.get("volume") or 0), list(p.get("deltas", []))
        if "volume" in p:
            d = vol - p["volume"]
            if len(deltas) >= 3:
                normal = statistics.median(deltas)
                if d >= SPIKE_MIN and d >= SPIKE_X * max(normal, 1):
                    alerts.append(f"{head}\n📈 Pic de volume\n+{d:,.0f} $ depuis le dernier passage (normal : ~{normal:,.0f} $)\n{link}")
            deltas = (deltas + [d])[-30:]
        prices = {}
        for m in ev.get("markets") or []:
            if PROPS.match(m.get("question") or ""):
                continue
            for o, x in zip(jl(m.get("outcomes")), jl(m.get("outcomePrices"))):
                k, x = f"{m.get('conditionId')}|{o}", float(x)
                prices[k] = x
                old = p.get("prices", {}).get(k)
                if old is not None and x - old >= PRICE_MOVE:  # hausse = l'argent arrive sur cette option
                    alerts.append(f"{head}\n⚡ Forte hausse de prix\n👉 Option : {o}\nMarché : {m.get('question', '')}\nPrix : {old*100:.0f} % → {x*100:.0f} %\n{link}")
        new[eid] = {"volume": vol, "deltas": deltas, "prices":
