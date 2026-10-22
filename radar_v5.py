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
SMALL = set(x.strip() for x in env("SMALL_LEAGUES", "chi").split(",") if x.strip())  # petits championnats (codes)
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


def fetch_trades(ids, now, min_usd):
    out = []
    for off in (0, 500, 1000):
        page = get(f"{DATA}/trades", eventId=ids, filterType="CASH", filterAmount=min_usd, limit=500, offset=off)
        out += page
        if len(page) < 500 or int(page[-1].get("timestamp") or now) < now - WINDOW:
            break
    return out


def fmt(g):
    p = g["price"]
    title = "🎯 MONTANTE — favori très misé" if g["kind"] == "favori" else "🐋 Gros paris détectés"
    lines = [g["head"], title, f"👉 Option : {g['outcome']}", f"Marché : {g['market']}",
             f"{g['n']} paris, total ≈ {g['total']:,.0f} $ (le plus gros : {g['biggest']:,.0f} $)",
             f"Prix actuel : {p*100:.0f} % (cote ≈ {1/p:.2f})" if p > 0 else "Prix actuel : ?"]
    if g.get("share") is not None and g["kind"] != "favori":
        lines.append(f"Poids : {g['share']*100:.0f} % du volume de ce marché")
    if g["small"]:
        lines.append("⚠️ Petit championnat : marché peu échangé, signal fragile")
    lines.append(f"https://polymarket.com/event/{g['slug']}")
    return "\n".join(lines)


def scan_trades(events, seen, first, now):
    """Regroupe les nouveaux achats par option, puis ne GARDE À L'ENVOI que les signaux pertinents.
    Tous les signaux sont suivis en silence pour comparer pertinents / filtrés."""
    ids = ",".join(str(e["id"]) for e in events)
    by_slug = {e.get("slug"): e for e in events}
    mvol = {m.get("conditionId"): float(m.get("volume") or 0) for e in events for m in e.get("markets") or []}
    groups = {}
    for t in fetch_trades(ids, now, min(MIN_TRADE, SMALL_MIN)):
        key = f"{t.get('transactionHash')}:{t.get('asset')}:{t.get('size')}"
        if key in seen:
            continue
        seen[key] = now
        ts = int(t.get("timestamp") or now)
        if first or t.get("side") != "BUY" or ts < now - WINDOW or PROPS.match(t.get("title") or ""):
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
    return f"🔎 Championnats de football trouvés (code : nombre de matchs)\n{liste}\nEnvoie-moi une capture : on choisira les petits championnats à surveiller."


SIZES = ["< 5 000 $", "5 000 à 10 000 $", "≥ 10 000 $", "taille inconnue"]
CATS = {"pertinent": "✅ Signaux envoyés (pertinents)", "filtre": "🔇 Signaux filtrés (non envoyés)",
        "fav": "🎯 Favoris (montante)", "petits": "🔎 Petits championnats", "ancien": "Anciens signaux (avant le filtre)"}


def record(stats, tr, won):
    """Ajoute un signal réglé au bilan. Simulation : 1 unité misée au prix de l'alerte."""
    price = tr.get("price") or 0
    if not 0.01 < price < 0.99:
        return
    gain = (1 / price - 1) if won else -1.0
    total = tr.get("total")
    size = SIZES[3] if total is None else SIZES[0] if total < 5000 else SIZES[1] if total < 10000 else SIZES[2]
    cat = "ancien" if "sent" not in tr else "fav" if tr.get("kind") == "favori" else "pertinent" if tr["sent"] else "filtre"
    cats = [cat] + (["petits"] if tr.get("small") else [])
    buckets = [stats.setdefault("all", {}), stats.setdefault("sport", {}).setdefault(tr.get("sport", "?"), {}),
               stats.setdefault("size", {}).setdefault(size, {})]
    buckets += [stats.setdefault("cat", {}).setdefault(c, {}) for c in cats]
    for b in buckets:
        b["n"] = b.get("n", 0) + 1
        b["w"] = b.get("w", 0) + (1 if won else 0)
        b["gain"] = round(b.get("gain", 0.0) + gain, 4)
        b["psum"] = round(b.get("psum", 0.0) + price, 4)


def line(label, b):
    n, w = b.get("n", 0), b.get("w", 0)
    return f"{label} : {n} signaux → ✅ {w} / ❌ {n - w} ({b.get('gain', 0):+.1f} unités)"


def report(stats):
    a = stats.get("all", {})
    n = a.get("n", 0)
    if not n:
        return None
    w = a["w"]
    msg = [f"📊 Bilan des signaux ({n} réglés)", f"✅ {w} gagnants ({w / n * 100:.0f} %) · ❌ {n - w} perdants",
           f"Gain simulé : {a['gain']:+.1f} unités en misant 1 unité sur chaque signal ({a['gain'] / n * 100:+.0f} % par signal)",
           "", "Par catégorie :"]
    for k, label in CATS.items():
        b = stats.get("cat", {}).get(k)
        if not b:
            continue
        msg.append(line(label, b))
        if k == "fav":
            real, implied = b["w"] / b["n"], b.get("psum", 0) / b["n"]
            msg.append(f"   Réussite réelle {real * 100:.0f} % vs prix moyen {implied * 100:.0f} %")
            msg.append(f"   10 paliers d'affilée : {real ** 10 * 100:.0f} % de chances" if b["n"] >= 30
                       else "   (moins de 30 favoris : estimation des paliers pas encore fiable)")
    msg += ["", "Par sport :"] + [line(k, v) for k, v in sorted(stats.get("sport", {}).items())]
    msg += ["", "Par montant du signal :"] + [line(k, stats["size"][k]) for k in SIZES if k in stats.get("size", {})]
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
            if not tr.get("sent", True):
                out.pop()  # signal filtré : pas de message, seulement le bilan
            del tracked[k]
    return out


def main():
    now = int(time.time())
    state = load_state()
    first = not state
    events, counts, universe = fetch_events()
    seen, tracked, stats = state.get("seen", {}), state.get("tracked", {}), state.get("stats", {})
    alerts, new_events = [], state.get("events", {})
    if events:
        a1, new_events = scan_events(events, state.get("events", {}), now)
        try:
            a2 = scan_trades(events, seen, first, now)
        except Exception as e:
            print(f"[warn] trades: {e}", flush=True)
            a2 = []
        alerts = [] if first else [{"text": t, "send": True} for t in a1] + a2
    results = [] if first else check_results(tracked, now, stats)
    if first:
        notify(f"✅ Radar démarré. {len(events)} matchs surveillés.\nÉvénements trouvés par tag : {counts}")
    discovered = state.get("discovered", False)
    if events and not discovered:
        notify(discover(universe))
        discovered = True
    sent = 0
    for a in alerts:
        if a.get("track"):  # tous les signaux sont suivis, envoyés ou non
            tracked.setdefault(a["track"][0], dict(a["track"][1], ts=now))
        if a.get("send") and sent < 10:
            notify(a["text"])
            sent += 1
    for r in results[:10]:
        notify(r)
    day, last = dt.datetime.fromtimestamp(now, dt.timezone.utc), state.get("last_report", "")
    if not first and day.hour >= 8 and last != day.strftime("%Y-%m-%d") and stats.get("all"):
        notify(report(stats))  # bilan complet, une fois par jour vers 8 h (heure du Mali)
        last = day.strftime("%Y-%m-%d")
    save_state({"events": new_events, "seen": dict(list(seen.items())[-6000:]), "tracked": tracked,
                "stats": stats, "last_report": last, "discovered": discovered, "ts": now})
    print(f"OK — {len(events)} matchs, {sent} alerte(s) envoyée(s), {len(alerts) - sent} suivie(s) en silence, {len(results)} résultat(s)", flush=True)


if __name__ == "__main__":
    main()
