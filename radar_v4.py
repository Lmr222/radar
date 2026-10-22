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
    "atp": "Tennis ATP (hommes) · 🌍 circuit mondial", "wta": "Tennis WTA (femmes) · 🌍 circuit mondial",
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
        data = [ev for ev in data if DATED.search(ev.get("slug") or "")]  # on ignore les paris à long terme
        counts[tag] = len(data)
        for ev in data:
            ev.setdefault("_sport", SPORTS.get(tag, tag.upper()))
            ev.setdefault("_league", league_of(ev))
            events.setdefault(ev["id"], ev)
    top = sorted(events.values(), key=lambda e: float(e.get("volume24hr") or 0), reverse=True)
    return top[:MAX_EVENTS], counts


def scan_events(events, prev, now):
    """Pics de volume et variations de prix, par comparaison avec le passage précédent."""
    alerts, new = [], {}
    for ev in events:
        eid, p = str(ev["id"]), prev.get(str(ev["id"]), {})
        title = ev.get("title", "?")
        link = f"https://polymarket.com/event/{ev.get('slug', '')}"
        head = f"{ev.get('_sport', '')} — {ev.get('_league', '')}\n{title}"
        vol, deltas = float(ev.get("volume") or 0), list(p.get("deltas", []))
        if "volume" in p and "ts" in p and now - p["ts"] <= MAX_GAP:  # sinon : passages manqués, on ignore
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
        new[eid] = {"volume": vol, "deltas": deltas, "prices": prices, "ts": now}
    return alerts, new


def scan_trades(events, seen, first):
    """Gros paris : on regroupe les nouveaux achats par option."""
    ids = ",".join(str(e["id"]) for e in events)
    by_slug = {e.get("slug"): e for e in events}
    trades = get(f"{DATA}/trades", eventId=ids, filterType="CASH", filterAmount=MIN_TRADE, limit=500)
    groups = {}
    for t in trades:
        key = f"{t.get('transactionHash')}:{t.get('asset')}:{t.get('size')}"
        if key in seen:
            continue
        seen[key] = int(time.time())
        if first or t.get("side") != "BUY" or PROPS.match(t.get("title") or ""):  # 1er passage = historique, on n'alerte pas
            continue
        cash = float(t["size"]) * float(t["price"])
        ev = by_slug.get(t.get("eventSlug"), {})
        g = groups.setdefault((t["conditionId"], t["outcome"]), dict(
            head=f"{ev.get('_sport', '')} — {ev.get('_league', '')}\n{ev.get('title') or t.get('title', '?')}",
            cid=t["conditionId"], sport=ev.get("_sport", "?"), market=t.get("title", "?"), outcome=t["outcome"], slug=t.get("eventSlug", ""),
            total=0.0, n=0, biggest=0.0, price=float(t["price"])))
        g["total"] += cash
        g["n"] += 1
        g["biggest"] = max(g["biggest"], cash)
    alerts = []
    for g in groups.values():
        if g["biggest"] >= WHALE or g["total"] >= CLUSTER:
            odds = f" (cote ≈ {1/g['price']:.2f})" if g["price"] > 0 else ""
            text = (
                f"{g['head']}\n🐋 Gros paris détectés\n👉 Option : {g['outcome']}\nMarché : {g['market']}\n"
                f"{g['n']} paris, total ≈ {g['total']:,.0f} $ (le plus gros : {g['biggest']:,.0f} $)\n"
                f"Prix actuel : {g['price']*100:.0f} %{odds}\nhttps://polymarket.com/event/{g['slug']}")
            alerts.append({"text": text, "track": (f"{g['cid']}|{g['outcome']}", dict(
                head=g["head"], market=g["market"], outcome=g["outcome"], slug=g["slug"], price=g["price"], sport=g["sport"], total=round(g["total"])))})
    return alerts


SIZES = ["< 5 000 $", "5 000 à 10 000 $", "≥ 10 000 $", "taille inconnue"]


def record(stats, tr, won):
    """Ajoute un signal réglé au bilan. Simulation : 1 unité misée au prix de l'alerte."""
    price = tr.get("price") or 0
    if not 0.01 < price < 0.99:
        return
    gain = (1 / price - 1) if won else -1.0
    total = tr.get("total")
    size = SIZES[3] if total is None else SIZES[0] if total < 5000 else SIZES[1] if total < 10000 else SIZES[2]
    for b in (stats.setdefault("all", {}),
              stats.setdefault("sport", {}).setdefault(tr.get("sport", "?"), {}),
              stats.setdefault("size", {}).setdefault(size, {})):
        b["n"] = b.get("n", 0) + 1
        b["w"] = b.get("w", 0) + (1 if won else 0)
        b["gain"] = round(b.get("gain", 0.0) + gain, 4)


def line(label, b):
    n, w = b.get("n", 0), b.get("w", 0)
    return f"{label} : {n} signaux → ✅ {w} / ❌ {n - w} ({b.get('gain', 0):+.1f} unités)"


def report(stats):
    a = stats.get("all", {})
    n = a.get("n", 0)
    if not n:
        return None
    w = a["w"]
    msg = [f"📊 Bilan des gros paris 🐋 ({n} signaux réglés)",
           f"✅ {w} gagnants ({w / n * 100:.0f} %) · ❌ {n - w} perdants",
           f"Gain simulé : {a['gain']:+.1f} unités en misant 1 unité sur chaque signal ({a['gain'] / n * 100:+.0f} % par signal)",
           "", "Par sport :"]
    msg += [line(k, v) for k, v in sorted(stats.get("sport", {}).items())]
    msg += ["", "Par montant du signal :"]
    msg += [line(k, stats["size"][k]) for k in SIZES if k in stats.get("size", {})]
    if n < 100:
        msg.append("\n⚠️ Moins de 100 signaux : trop peu pour conclure, le hasard pèse encore beaucoup.")
    msg.append("Calcul au prix Polymarket, pas à la cote de ton bookmaker.")
    return "\n".join(msg)


def check_results(tracked, now, stats):
    """Pour chaque gros pari signalé : annonce ✅ ou ❌ quand le marché est réglé."""
    out, by_slug = [], {}
    for k, tr in list(tracked.items()):
        if now - tr["ts"] > 7 * 86400:
            del tracked[k]  # trop ancien, on abandonne
        elif now - tr["ts"] > 1800:  # un match ne se règle jamais en moins de 30 minutes
            by_slug.setdefault(tr["slug"], []).append(k)
    for slug, keys in by_slug.items():
        try:
            markets = {m.get("conditionId"): m for ev in get(f"{GAMMA}/events", slug=slug) for m in ev.get("markets") or []}
        except Exception as e:
            print(f"[warn] résultat {slug}: {e}", flush=True)
            continue
        for k in keys:
            tr, m = tracked[k], markets.get(k.split("|")[0])
            if not m or not m.get("closed"):
                continue
            prices = dict(zip(jl(m.get("outcomes")), [float(x) for x in jl(m.get("outcomePrices"))]))
            price = prices.get(tr["outcome"])
            if price is None or 0.01 < price < 0.99:
                continue  # pas encore réglé (ou marché annulé)
            verdict = "✅ Résultat final : alerte GAGNANTE" if price >= 0.99 else "❌ Résultat final : alerte PERDANTE"
            record(stats, tr, price >= 0.99)
            a = stats.get("all", {})
            bilan = f"Bilan : ✅ {a.get('w', 0)} gagnants / ❌ {a.get('n', 0) - a.get('w', 0)} perdants"
            out.append(f"{verdict}\n{tr['head']}\n👉 Option : {tr['outcome']}\nMarché : {tr['market']}\n"
                       f"Prix au moment de l'alerte : {tr['price']*100:.0f} %\nhttps://polymarket.com/event/{tr['slug']}\n{bilan}")
            del tracked[k]
    return out


def main():
    now = int(time.time())
    state = load_state()
    first = not state
    events, counts = fetch_events()
    seen, tracked, stats = state.get("seen", {}), state.get("tracked", {}), state.get("stats", {})
    alerts, new_events = [], state.get("events", {})
    if events:
        a1, new_events = scan_events(events, state.get("events", {}), now)
        try:
            a2 = scan_trades(events, seen, first)
        except Exception as e:
            print(f"[warn] trades: {e}", flush=True)
            a2 = []
        alerts = [] if first else [{"text": t} for t in a1] + a2
    results = [] if first else check_results(tracked, now, stats)
    if first:
        notify(f"✅ Radar démarré. {len(events)} matchs surveillés.\nÉvénements trouvés par tag : {counts}")
    for a in alerts[:10]:
        notify(a["text"])
        if a.get("track"):  # on mémorise pour annoncer le résultat final plus tard
            tracked.setdefault(a["track"][0], dict(a["track"][1], ts=now))
    for r in results[:10]:
        notify(r)
    day, last = dt.datetime.fromtimestamp(now, dt.timezone.utc), state.get("last_report", "")
    if not first and day.hour >= 8 and last != day.strftime("%Y-%m-%d") and stats.get("all"):
        notify(report(stats))  # bilan complet, une fois par jour vers 8 h (heure du Mali)
        last = day.strftime("%Y-%m-%d")
    save_state({"events": new_events, "seen": dict(list(seen.items())[-3000:]), "tracked": tracked,
                "stats": stats, "last_report": last, "ts": now})
    print(f"OK — {len(events)} matchs, {len(alerts)} alerte(s), {len(results)} résultat(s)", flush=True)


if __name__ == "__main__":
    main()
