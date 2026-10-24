#!/usr/bin/env python3
"""Radar de gros paris — Polymarket (données publiques, gratuites, sans clé).

Repère sur les marchés sportifs :
  🐋 un gros pari isolé, ou un afflux d'argent sur une même option
  📈 un pic de volume sur un match
  ⚡ une forte variation de prix
puis envoie une alerte Telegram. Le script ne parie JAMAIS : la décision t'appartient.

Usage local :  python radar.py --dry   (affiche les alertes, n'envoie rien)
"""
import datetime as dt
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
MAX_GAP = int(env("MAX_GAP_MIN", "30")) * 60  # écart max entre 2 passages pour juger un pic de volume
DATED = re.compile(r"\d{4}-\d{2}-\d{2}")     # vrai match = adresse contenant une date
WINDOW = int(env("WINDOW_MIN", "20")) * 60      # on ne compte que les paris des 20 dernières minutes
MID_LOW, MID_HIGH = float(env("MID_LOW", "0.20")), float(env("MID_HIGH", "0.80"))   # signaux « standard »
FAV_LOW, FAV_HIGH = float(env("FAV_LOW", "0.88")), float(env("FAV_HIGH", "0.97"))   # favoris (montante)
DOMINANCE = float(env("DOMINANCE", "3"))        # un camp doit peser 3 fois l'autre
REL_MIN = float(env("REL_MIN", "0.03"))         # et au moins 3 % du volume du marché
SMALL = set(x.strip() for x in env("SMALL_LEAGUES", "chi,chi2,scop,sclc,kor,kor2,ja2,ukr1,argpn,uru1,svk1,gtm,nor,nor2,swe,swe2,kaz1,chl2,col2,bra3,bul,srb,slo,hr1,irl1,pol,isl1,den").split(",") if x.strip())  # petits championnats (codes)
SMALL_MIN = float(env("SMALL_MIN_USD", "200"))
SMALL_WHALE = float(env("SMALL_WHALE_USD", "500"))
SMALL_CLUSTER = float(env("SMALL_CLUSTER_USD", "1000"))
SPORTS = {"soccer": "⚽ FOOTBALL", "epl": "⚽ FOOTBALL", "champions-league": "⚽ FOOTBALL",
          "basketball": "🏀 BASKET", "nba": "🏀 BASKET", "tennis": "🎾 TENNIS"}
# paris sur un joueur précis, ex. « Naz Reid: Points O/U 17.5 » (trop de bruit, ignorés)
PROPS = re.compile(r"^(?!.*\bvs\b)[^:]+:.*O/U", re.I)
# championnat + pays, d'après le début de l'adresse du match (ex. nba-gsw-lal-2026-10-21)
LEAGUES = {
    "nba": "NBA · 🇺🇸 États-Unis", "wnba": "WNBA · 🇺🇸 États-Unis",
    "ncaab": "Basket universitaire NCAA · 🇺🇸 États-Unis", "cbb": "Basket universitaire NCAA · 🇺🇸 États-Unis",
    "euroleague": "EuroLeague · 🌍 Europe",
    "epl": "Premier League · 🏴 Angleterre", "lal": "La Liga · 🇪🇸 Espagne", "fl1": "Ligue 1 · 🇫🇷 France",
    "bun": "Bundesliga · 🇩🇪 Allemagne", "sea": "Serie A · 🇮🇹 Italie",
    "ucl": "Ligue des champions · 🌍 Europe", "uel": "Ligue Europa · 🌍 Europe",
    "chi": "Super League · 🇨🇳 Chine", "atp": "Tennis ATP (hommes) · 🌍 circuit mondial", "wta": "Tennis WTA (femmes) · 🌍 circuit mondial",
}
GENERIC = {"sports", "games", "basketball", "soccer", "tennis", "football", "hide-from-new", "recurring"}


def league_of(ev):
    prefix = (ev.get("slug") or "").split("-")[0].lower()
    if prefix in LEAGUES:
        return LEAGUES[prefix]
    for tg in ev.get("tags") or []:  # sinon : on prend l'étiquette de Polymarket
        if isinstance(tg, dict) and tg.get("label") and (tg.get("slug") or "").lower() not in GENERIC:
            return tg["label"]
    return prefix.upper()


def pari_lisible(q, o):
    """Rend l'option compréhensible, surtout pour les paris OUI/NON du football."""
    yes, no = o.strip().lower() == "yes", o.strip().lower() == "no"
    if not (yes or no):
        return o
    m = re.match(r"^Will (.+?) win\b", q, re.I)
    if m:
        return f"{m.group(1)} gagne" if yes else f"{m.group(1)} ne gagne PAS (nul ou défaite)"
    if re.search(r"end in a draw", q, re.I):
        return "Match nul" if yes else "Pas de match nul"
    if re.search(r"both teams to score", q, re.I):
        return "Les deux équipes marquent" if yes else "Au moins une équipe ne marque pas"
    return f"{'OUI' if yes else 'NON'} — {q}"


def get(url, **params):
    r = requests.get(url, params=params, timeout=30, headers={"User-Agent": "bet-radar/1.0"})
    r.raise_for_status()
    return r.json()


def jl(x):
    """Gamma renvoie parfois les listes sous forme de texte JSON."""
    return json.loads(x) if isinstance(x, str) else (x or [])


def notify(text, reply_to=None):
    print(text, "\n---", flush=True)
    if DRY or not (TOKEN and CHAT):
        return None
    payload = {"chat_id": CHAT, "text": text, "disable_web_page_preview": True}
    if reply_to:  # le message cite le signal précédent : un appui dessus y ramène
        payload["reply_parameters"] = {"message_id": reply_to, "allow_sending_without_reply": True}
    try:
        r = requests.post(f"https://api.telegram.org/bot{TOKEN}/sendMessage", json=payload, timeout=30)
        r.raise_for_status()
        return r.json().get("result", {}).get("message_id")
    except Exception as e:
        print(f"[warn] Telegram: {e}", flush=True)
        return None


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
        data = []
        for off in (0, 100, 200):
            try:
                page = get(f"{GAMMA}/events", tag_slug=tag, active="true", closed="false", limit=100, offset=off)
            except Exception as e:
                print(f"[warn] tag {tag}: {e}", flush=True)
                break
            data += page
            if len(page) < 100:
                break
        data = [ev for ev in data if DATED.search(ev.get("slug") or "")]  # on ignore les paris à long terme
        counts[tag] = len(data)
        for ev in data:
            ev.setdefault("_sport", SPORTS.get(tag, tag.upper()))
            ev.setdefault("_league", league_of(ev))
            ev["_small"] = (ev.get("slug") or "").split("-")[0].lower() in SMALL
            events.setdefault(ev["id"], ev)
    ranked = sorted(events.values(), key=lambda e: float(e.get("volume24hr") or 0), reverse=True)
    top = ranked[:MAX_EVENTS] + [e for e in ranked[MAX_EVENTS:] if e["_small"]]
    return top, counts, events


def scan_events(events, prev, now):
    """Pics de volume et variations de prix, par comparaison avec le passage précédent."""
    alerts, new = [], {}
    for ev in events:
        eid, p = str(ev["id"]), prev.get(str(ev["id"]), {})
        title = ev.get("title", "?")
        link = f"https://polymarket.com/event/{ev.get('slug', '')}"
        head = f"{ev.get('_sport', '')} — {ev.get('_league', '')}\n{title}"
        vol, deltas = float(ev.get("volume") or 0), list(p.get("deltas", []))
        gap = now - p["ts"] if "ts" in p else 0
        if "volume" in p and 0 < gap <= 6 * 3600:  # écart énorme (> 6 h) : on ignore
            d = vol - p["volume"]
            rate = d * 600 / max(gap, 60)  # montant ramené à 10 minutes
            if len(deltas) >= 3:
                normal = statistics.median(deltas)
                if d >= SPIKE_MIN and rate >= SPIKE_X * max(normal, 1):
                    alerts.append(f"{head}\n📈 Pic de volume\n+{d:,.0f} $depuis le dernier passage (normal : ~{normal:,.0f}$ par 10 min)\n{link}")
            deltas = (deltas + [rate])[-30:]
        prices = {}
        for m in ev.get("markets") or []:
            if PROPS.match(m.get("question") or ""):
                continue
            for o, x in zip(jl(m.get("outcomes")), jl(m.get("outcomePrices"))):
                k, x = f"{m.get('conditionId')}|{o}", float(x)
                prices[k] = x
                old = p.get("prices", {}).get(k)
                if old is not None and x - old >= PRICE_MOVE:  # hausse = l'argent arrive sur cette option
                    alerts.append(f"{head}\n⚡ Forte hausse de prix\n👉 Option : {pari_lisible(m.get('question', ''), o)}\nMarché : {m.get('question', '')}\nPrix : {old*100:.0f} % → {x*100:.0f} %\n{link}")
        new[eid] = {"volume": vol, "deltas": deltas, "prices": prices, "ts": now}
    return alerts, new


def fetch_trades(ids, now, min_usd, window):
    out = []
    for off in range(0, 1500, 500):
        try:
            page = get(f"{DATA}/trades", eventId=ids, filterType="CASH", filterAmount=min_usd, limit=500, offset=off)
            if not page:
                break
            out += page
            if len(page) < 500 or int(page[-1].get("timestamp") or now) < now - window:
                break
        except Exception as e:
            print(f"[warn] trades offset {off}: {e}", flush=True)
            break
    return out


def fmt(g):
    p = g["price"]
    title = "🎯 MONTANTE — favori très misé" if g["kind"] == "favori" else "🐋 Gros paris détectés"
    lines = [g["head"], title, f"👉 Option : {pari_lisible(g['market'], g['outcome'])}", f"Marché : {g['market']}",
             f"{g['n']} paris, total ≈ {g['total']:,.0f} $(le plus gros : {g['biggest']:,.0f}$)",
             f"Prix actuel : {p*100:.0f} % (cote ≈ {1/p:.2f})" if p > 0 else "Prix actuel : ?"]
    if g.get("share") is not None and g["kind"] != "favori":
        lines.append(f"Poids : {g['share']*100:.0f} % du volume de ce marché")
    if g["small"]:
        lines.append("⚠️ Petit championnat : marché peu échangé, signal fragile")
    lines.append(f"https://polymarket.com/event/{g['slug']}")
    return "\n".join(lines)


def scan_trades(events, seen, first, now, window):
    """Regroupe les nouveaux achats par option, puis ne GARDE À L'ENVOI que les signaux pertinents.
    Tous les signaux sont suivis en silence pour comparer pertinents / filtrés."""
    ids = ",".join(str(e["id"]) for e in events)
    by_slug = {e.get("slug"): e for e in events}
    mvol = {m.get("conditionId"): float(m.get("volume") or 0) for e in events for m in e.get("markets") or []}
    groups = {}
    for t in fetch_trades(ids, now, min(MIN_TRADE, SMALL_MIN), window):
        key = f"{t.get('transactionHash')}:{t.get('asset')}:{t.get('size')}"
        if key in seen:
            continue
        seen[key] = now
        ts = int(t.get("timestamp") or now)
        if first or t.get("side") != "BUY" or ts < now - window or PROPS.match(t.get("title") or ""):
            continue
        ev = by_slug.get(t.get("eventSlug"), {})
        cash = float(t["size"]) * float(t["price"])
        if cash < (SMALL_MIN if ev.get("_small") else MIN_TRADE):
            continue
        g = groups.setdefault((t["conditionId"], t["outcome"]), dict(
            head=f"{ev.get('_sport', '')} — {ev.get('_league', '')}\n{ev.get('title') or t.get('title', '?')}",
            cid=t["conditionId"], sport=ev.get("_sport", "?"), small=bool(ev.get("_small")),
            market=t.get("title", "?"), outcome=t["outcome"], slug=t.get("eventSlug", ""),
            total=0.0, n=0, biggest=0.0, price=float(t["price"])))
        g["total"] += cash
        g["n"] += 1
        g["biggest"] = max(g["biggest"], cash)
    cands, best = [], {}
    for (cid, outcome), g in groups.items():
        whale, cluster = (SMALL_WHALE, SMALL_CLUSTER) if g["small"] else (WHALE, CLUSTER)
        if g["biggest"] < whale and g["total"] < cluster:
            continue
        opp = sum(o["total"] for o in groups.values() if o["cid"] == cid and o is not g)
        p = g["price"]
        kind = "favori" if FAV_LOW <= p <= FAV_HIGH else "standard" if MID_LOW <= p <= MID_HIGH else "autre"
        share = g["total"] / mvol[cid] if mvol.get(cid) else None
        g.update(kind=kind, share=share, key=f"{cid}|{outcome}")
        if kind != "autre" and g["total"] >= DOMINANCE * opp and (kind == "favori" or share is None or share >= REL_MIN):
            k = (g["slug"], kind)  # un seul message par match et par type : le plus fort
            if k not in best or g["total"] > best[k]["total"]:
                best[k] = g
        cands.append(g)
    out = []
    for g in cands:
        send = best.get((g["slug"], g["kind"])) is g
        info = dict(head=g["head"], market=g["market"], outcome=g["outcome"], slug=g["slug"], price=g["price"],
                    sport=g["sport"], total=round(g["total"]), kind=g["kind"], sent=send, small=g["small"])
        out.append({"text": fmt(g) if send else None, "send": send, "track": (g["key"], info)})
    return out


def discover(universe):
    codes = {}
    for e in universe.values():
        if "FOOTBALL" in e.get("_sport", ""):
            c = (e.get("slug") or "").split("-")[0].lower()
            codes[c] = codes.get(c, 0) + 1
    liste = ", ".join(f"{c}: {n}" for c, n in sorted(codes.items(), key=lambda x: -x[1]))
    return f"🔎 Championnats
