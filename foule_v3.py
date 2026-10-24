#!/usr/bin/env python3
"""Options les plus jouées — Polymarket (données publiques, gratuites, sans clé).

Bot indépendant du radar de gros paris. Pour chaque match du jour, il compte les paris
(achats de 200 $ ou plus) par option, puis t'envoie deux fois par jour l'option qui a reçu
le plus de paris. Il suit aussi les résultats pour savoir si suivre la foule est rentable.
Il ne parie JAMAIS : la décision t'appartient.

Usage local :  python foule.py --dry   (affiche les messages, n'envoie rien)
"""
import datetime as dt
import json
import os
import re
import sys
import time
from pathlib import Path

import requests


def env(name, default):
    return os.getenv(name) or default


GAMMA, DATA = "https://gamma-api.polymarket.com", "https://data-api.polymarket.com"
STATE_FILE = Path(env("STATE_FILE", "state_foule/state.json"))
TAGS = [t.strip() for t in env("SPORT_TAGS", "soccer,basketball,nba,tennis,epl,champions-league").split(",") if t.strip()]
MAX_EVENTS = int(env("MAX_EVENTS", "60"))
MIN_TRADE = float(env("MIN_TRADE_USD", "200"))     # les paris plus petits ne sont pas comptés
MIN_BETS = int(env("MIN_BETS", "8"))               # un match doit avoir au moins 8 paris comptés
DIGEST_N = int(env("DIGEST_N", "8"))               # nombre de matchs par récapitulatif
DIGEST_HOURS = set(int(x) for x in env("DIGEST_HOURS", "10,17").split(",") if x.strip())  # UTC = heure du Mali
REPORT_HOUR = int(env("REPORT_HOUR", "8"))         # bilan quotidien
TOKEN, CHAT = os.getenv("TELEGRAM_TOKEN"), os.getenv("TELEGRAM_CHAT_ID")
DRY = "--dry" in sys.argv
SPORTS = {"soccer": "⚽ FOOTBALL", "epl": "⚽ FOOTBALL", "champions-league": "⚽ FOOTBALL",
          "basketball": "🏀 BASKET", "nba": "🏀 BASKET", "tennis": "🎾 TENNIS"}
DATED = re.compile(r"\d{4}-\d{2}-\d{2}")           # vrai match = adresse contenant une date
PROPS = re.compile(r"^(?!.*\bvs\b)[^:]+:.*O/U", re.I)   # paris sur un joueur précis : ignorés
SECONDARY = re.compile(r"\b(1H|2H|first half|second half|to score first|completed match|set \d|game \d|[1-4]Q|quarter)\b", re.I)
LEAGUES = {
    "nba": "NBA · 🇺🇸 États-Unis", "wnba": "WNBA · 🇺🇸 États-Unis", "euroleague": "EuroLeague · 🌍 Europe",
    "epl": "Premier League · 🏴 Angleterre", "lal": "La Liga · 🇪🇸 Espagne", "fl1": "Ligue 1 · 🇫🇷 France",
    "bun": "Bundesliga · 🇩🇪 Allemagne", "sea": "Serie A · 🇮🇹 Italie", "ucl": "Ligue des champions · 🌍 Europe",
    "uel": "Ligue Europa · 🌍 Europe", "chi": "Super League · 🇨🇳 Chine",
    "atp": "Tennis ATP (hommes) · 🌍 circuit mondial", "wta": "Tennis WTA (femmes) · 🌍 circuit mondial",
}
GENERIC = {"sports", "games", "basketball", "soccer", "tennis", "football", "hide-from-new", "recurring"}
SHARES = ["< 60 %", "60 à 75 %", "≥ 75 %"]


def league_of(ev):
    prefix = (ev.get("slug") or "").split("-")[0].lower()
    if prefix in LEAGUES:
        return LEAGUES[prefix]
    for tg in ev.get("tags") or []:
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
    r = requests.get(url, params=params, timeout=30, headers={"User-Agent": "bet-foule/1.0"})
    r.raise_for_status()
    return r.json()


def jl(x):
    return json.loads(x) if isinstance(x, str) else (x or [])


def notify(text):
    print(text, "\n---", flush=True)
    if DRY or not (TOKEN and CHAT):
        return
    try:
        requests.post(f"https://api.telegram.org/bot{TOKEN}/sendMessage",
                      json={"chat_id": CHAT, "text": text, "disable_web_page_preview": True}, timeout=30).raise_for_status()
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
        data = [ev for ev in data if DATED.search(ev.get("slug") or "")]
        counts[tag] = len(data)
        for ev in data:
            ev.setdefault("_sport", SPORTS.get(tag, tag.upper()))
            ev.setdefault("_league", league_of(ev))
            events.setdefault(ev["id"], ev)
    ranked = sorted(events.values(), key=lambda e: float(e.get("volume24hr") or 0), reverse=True)
    return ranked[:MAX_EVENTS], counts


def trade_key(t):
    return f"{t.get('transactionHash')}:{t.get('asset')}:{t.get('size')}"


def fetch_new_trades(ids, seen):
    new = []
    for off in range(0, 4000, 500):
        page = get(f"{DATA}/trades", eventId=ids, filterType="CASH", filterAmount=MIN_TRADE, limit=500, offset=off)
        fresh = [t for t in page if trade_key(t) not in seen]
        new += fresh
        if len(page) < 500 or len(fresh) < len(page):  # on a rejoint des paris déjà comptés
            break
    return new


def accumulate(acc, t, now):
    """Compte chaque achat par option et par match, depuis le début de la surveillance."""
    title = t.get("title") or ""
    if t.get("side") != "BUY" or PROPS.match(title) or SECONDARY.search(title) or not t.get("eventSlug"):
        return
    a = acc.setdefault(t["eventSlug"], {"o": {}})
    a["ts"] = now
    o = a["o"].setdefault(f"{t['conditionId']}|{t['outcome']}", {"n": 0, "usd": 0.0, "q": title})
    price, ts = float(t["price"]), int(t.get("timestamp") or now)
    o["n"] += 1
    o["usd"] = round(o["usd"] + float(t["size"]) * price, 2)
    if ts >= o.get("t", 0):
        o["t"], o["p"] = ts, price


def digest(events, acc, tracked, now, day, settled):
    today, tomorrow = day.strftime("%Y-%m-%d"), (day + dt.timedelta(days=1)).strftime("%Y-%m-%d")
    rows = []
    for ev in events:
        slug = ev.get("slug") or ""
        a, m = acc.get(slug), DATED.search(slug)
        if not a or not m or m.group(0) not in (today, tomorrow):
            continue
        opts = a["o"]
        total_n = sum(o["n"] for o in opts.values())
        if total_n < MIN_BETS:
            continue
        mtot = {}
        for k, o in opts.items():
            c = k.split("|")[0]
            mtot[c] = mtot.get(c, 0) + o["n"]
        k, o = max(opts.items(), key=lambda kv: kv[1]["n"])
        km, om = max(opts.items(), key=lambda kv: kv[1]["usd"])
        rows.append((total_n, ev, k, o, km, om, mtot))
    rows.sort(key=lambda r: r[0], reverse=True)
    blocks = []
    for total_n, ev, k, o, km, om, mtot in rows[:DIGEST_N]:
        cid, outcome = k.split("|", 1)
        pr = o.get("p") or 0
        share = o["n"] / mtot[cid] * 100
        head = f"{ev.get('_sport', '')} — {ev.get('_league', '')}\n{ev.get('title', '?')}"
        cote = f" · prix {pr * 100:.0f} % (cote ≈ {1 / pr:.2f})" if 0 < pr < 1 else ""
        b = [head, f"👉 {pari_lisible(o['q'], outcome)}", f"   Marché : {o['q']}", f"   {o['n']} paris · {o['usd']:,.0f} $ · {share:.0f} % des paris de ce marché{cote}"]
        if km != k:
            b.append(f"   💰 Plus d'argent : {pari_lisible(om['q'], km.split('|', 1)[1])} ({om['usd']:,.0f} $)")
        b.append(f"   https://polymarket.com/event/{ev['slug']}")
        blocks.append("\n".join(b))
        if k not in settled:  # une option déjà réglée n'est jamais recomptée
            tracked.setdefault(k, dict(title=ev.get("title", "?"), market=o["q"], outcome=outcome, slug=ev["slug"], price=pr,
                                   sport=ev.get("_sport", "?"), share=round(share), ts=now))
    if not blocks:
        return []
    out, cur = [], f"📌 Options les plus jouées — {day.strftime('%H:%M')} (heure du Mali)"
    for b in blocks:
        if len(cur) + len(b) > 3500:
            out.append(cur)
            cur = "📌 (suite)"
        cur += "\n\n" + b
    out.append(cur)
    return out


def record(stats, tr, won):
    price = tr.get("price") or 0
    if not 0.01 < price < 0.99:
        return
    gain = (1 / price - 1) if won else -1.0
    sh = tr.get("share", 0)
    size = SHARES[0] if sh < 60 else SHARES[1] if sh < 75 else SHARES[2]
    for b in (stats.setdefault("all", {}), stats.setdefault("sport", {}).setdefault(tr.get("sport", "?"), {}),
              stats.setdefault("share", {}).setdefault(size, {})):
        b["n"] = b.get("n", 0) + 1
        b["w"] = b.get("w", 0) + (1 if won else 0)
        b["gain"] = round(b.get("gain", 0.0) + gain, 4)


def check_results(tracked, now, stats):
    """Renvoie une ligne par option dont le marché est réglé (✅ ou ❌)."""
    out, by_slug = [], {}
    for k, tr in list(tracked.items()):
        if now - tr["ts"] > 7 * 86400:
            del tracked[k]
        elif now - tr["ts"] > 1800:
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
                continue
            won = price >= 0.99
            record(stats, tr, won)
            stats.setdefault("done", {})[k] = now
            out.append(f"{'✅' if won else '❌'} {tr['title']} — {pari_lisible(tr['market'], tr['outcome'])} · prix {tr['price'] * 100:.0f} %")
            del tracked[k]
    return out


def line(label, b):
    n, w = b.get("n", 0), b.get("w", 0)
    return f"{label} : {n} → ✅ {w} / ❌ {n - w} ({b.get('gain', 0):+.1f} unités)"


def report(stats, lines):
    a = stats.get("all", {})
    n = a.get("n", 0)
    msg = ["📊 Bilan — options les plus jouées"]
    if not lines and not n:
        msg.append("Aucune option réglée pour le moment.")
    if lines:
        msg += ["", "Résultats récents :"] + lines[:25]
    if n:
        w = a["w"]
        msg += ["", f"Total : {n} options réglées · ✅ {w} ({w / n * 100:.0f} %) · ❌ {n - w}",
                f"Gain simulé : {a['gain']:+.1f} unités en misant 1 unité sur chacune ({a['gain'] / n * 100:+.0f} % par option)",
                "", "Par sport :"] + [line(k, v) for k, v in sorted(stats.get("sport", {}).items())]
        msg += ["", "Selon la part des paris sur l'option :"] + [line(k, stats["share"][k]) for k in SHARES if k in stats.get("share", {})]
        if n < 100:
            msg.append("\n⚠️ Moins de 100 options réglées : trop peu pour conclure, le hasard pèse encore beaucoup.")
        msg.append("Calcul au prix Polymarket, pas à la cote de ton bookmaker.")
    return "\n".join(msg)


def main():
    now = int(time.time())
    day = dt.datetime.fromtimestamp(now, dt.timezone.utc)
    state = load_state()
    first = not state
    events, counts = fetch_events()
    seen, tracked, stats, acc = state.get("seen", {}), state.get("tracked", {}), state.get("stats", {}), state.get("acc", {})
    pending = state.get("pending", [])
    if events:
        try:
            for t in fetch_new_trades(",".join(str(e["id"]) for e in events), seen):
                seen[trade_key(t)] = now
                accumulate(acc, t, now)
        except Exception as e:
            print(f"[warn] trades: {e}", flush=True)
    if not first:
        pending += check_results(tracked, now, stats)
    if first:
        notify(f"✅ Bot « Options les plus jouées » démarré. {len(events)} matchs surveillés.\n"
               f"Récapitulatifs à {', '.join(f'{h} h' for h in sorted(DIGEST_HOURS))} (heure du Mali), bilan vers {REPORT_HOUR} h.")
    done = state.get("digests", [])
    for h in sorted(DIGEST_HOURS):  # rattrapage : jusqu'à 4 h de retard si GitHub saute des passages
        tag = f"{day:%Y-%m-%d}-{h}"
        if not first and events and tag not in done and 0 <= day.hour - h <= 4:
            for m in digest(events, acc, tracked, now, day, stats.get("done", {})):
                notify(m)
            done.append(tag)
    runs = [r for r in state.get("runs", []) if r > now - 86400] + [now]
    last = state.get("last_report", "")
    if not first and day.hour >= REPORT_HOUR and last != day.strftime("%Y-%m-%d"):
        notify(report(stats, pending) + f"\n\nℹ️ Le bot a tourné {len(runs)} fois ces dernières 24 h (idéal : 144).")
        pending, last = [], day.strftime("%Y-%m-%d")
    stats["done"] = {k: v for k, v in stats.get("done", {}).items() if v > now - 7 * 86400}
    live = {e.get("slug") for e in events}
    acc = {k: v for k, v in acc.items() if k in live or now - v.get("ts", 0) < 172800}
    save_state({"seen": dict(list(seen.items())[-6000:]), "tracked": tracked, "stats": stats, "acc": acc,
                "pending": pending[-60:], "digests": done[-20:], "runs": runs[-200:], "last_report": last, "ts": now})
    print(f"OK — {len(events)} matchs, {len(tracked)} option(s) suivie(s)", flush=True)


if __name__ == "__main__":
    main()
