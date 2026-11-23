#!/usr/bin/env python3
"""
WoW: Forever Cloud Auto-Flipper (EU / Normal PvE / Allianz -> Ruleset 2, House 111)
Keine externen Pakete noetig (Python 3.10+ Standardbibliothek).
"""

import base64
import gzip
import json
import os
import sqlite3
import statistics
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import datetime, timezone

REGION = "eu"
NAMESPACE_DYNAMIC = f"dynamic-forever-{REGION}"
NAMESPACE_STATIC = f"static-forever-{REGION}"
RULESET_ID = 2
HOUSE_ID = 111
AH_CUT = 0.05
KEEP_DAYS = 14
DB_PATH = "wow_forever.db"
EXPORT_JSON_PATH = "flips_export.json"

WEEKDAYS_DE = ["Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"]


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts_utc INTEGER NOT NULL,
            weekday INTEGER NOT NULL,
            hour INTEGER NOT NULL,
            total_listings INTEGER NOT NULL
        );

        CREATE TABLE IF NOT EXISTS item_stats (
            snapshot_id INTEGER NOT NULL,
            item_id INTEGER NOT NULL,
            min_unit INTEGER NOT NULL,
            p10_unit INTEGER NOT NULL,
            median_unit INTEGER NOT NULL,
            total_qty INTEGER NOT NULL,
            listing_count INTEGER NOT NULL,
            est_sold_since_prev INTEGER DEFAULT 0,
            PRIMARY KEY (snapshot_id, item_id)
        );

        CREATE TABLE IF NOT EXISTS raw_last_snapshot (
            auction_id INTEGER PRIMARY KEY,
            item_id INTEGER NOT NULL,
            unit_price INTEGER NOT NULL,
            quantity INTEGER NOT NULL,
            time_left TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS item_meta (
            item_id INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            sell_price INTEGER DEFAULT 0,
            quality TEXT DEFAULT 'COMMON'
        );

        CREATE INDEX IF NOT EXISTS idx_item_stats_item ON item_stats(item_id);
        CREATE INDEX IF NOT EXISTS idx_snapshots_ts ON snapshots(ts_utc);
        """
    )
    conn.commit()


def http_get_json(url: str, headers: dict) -> dict:
    req = urllib.request.Request(url, headers={**headers, "Accept-Encoding": "gzip"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        raw = resp.read()
        if resp.headers.get("Content-Encoding") == "gzip":
            raw = gzip.decompress(raw)
        return json.loads(raw.decode("utf-8"))


def get_oauth_token() -> str:
    client_id = os.environ.get("BNET_CLIENT_ID", "").strip()
    client_secret = os.environ.get("BNET_CLIENT_SECRET", "").strip()
    if not client_id or not client_secret:
        raise RuntimeError("BNET_CLIENT_ID oder BNET_CLIENT_SECRET fehlt in den Secrets.")
    auth = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
    data = urllib.parse.urlencode({"grant_type": "client_credentials"}).encode()
    req = urllib.request.Request(
        "https://oauth.battle.net/token",
        data=data,
        headers={
            "Authorization": f"Basic {auth}",
            "Content-Type": "application/x-www-form-urlencoded",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=25) as resp:
        return json.loads(resp.read().decode("utf-8"))["access_token"]


def ensure_namespace(url: str, namespace: str) -> str:
    if "namespace=" in url:
        return url
    sep = "&" if "?" in url else "?"
    return f"{url}{sep}namespace={namespace}"


def fetch_auctions(token: str) -> list:
    headers = {"Authorization": f"Bearer {token}"}
    base = f"https://{REGION}.api.blizzard.com"
    idx_url = f"{base}/data/wow/ruleset/{RULESET_ID}/auctions/index?namespace={NAMESPACE_DYNAMIC}"
    index_data = http_get_json(idx_url, headers)

    house_href = None
    for entry in index_data.get("auctions", []):
        href = entry.get("key", {}).get("href", "")
        if f"/auctions/{HOUSE_ID}" in href or entry.get("id") == HOUSE_ID:
            house_href = href
            break

    if not house_href:
        house_href = f"{base}/data/wow/ruleset/{RULESET_ID}/auctions/{HOUSE_ID}"

    payload = http_get_json(ensure_namespace(house_href, NAMESPACE_DYNAMIC), headers)
    auctions = list(payload.get("auctions", []))

    comm_href = payload.get("commodities", {}).get("href")
    if comm_href:
        comm_payload = http_get_json(ensure_namespace(comm_href, NAMESPACE_DYNAMIC), headers)
        auctions.extend(comm_payload.get("auctions", []))

    return auctions


def fetch_missing_item_meta(conn: sqlite3.Connection, token: str, item_ids: list) -> None:
    if not item_ids:
        return
    cur = conn.cursor()
    placeholders = ",".join("?" * len(item_ids))
    existing = {
        row[0]
        for row in cur.execute(
            f"SELECT item_id FROM item_meta WHERE item_id IN ({placeholders})", item_ids
        )
    }
    missing = [iid for iid in item_ids if iid not in existing]
    if not missing:
        return

    headers = {"Authorization": f"Bearer {token}"}
    base = f"https://{REGION}.api.blizzard.com"
    namespaces = (NAMESPACE_STATIC, f"static-classic1x-{REGION}", f"static-{REGION}")

    for iid in missing[:60]:
        name = f"Item #{iid}"
        sell_price = 0
        quality = "COMMON"
        for ns in namespaces:
            try:
                url = f"{base}/data/wow/item/{iid}?namespace={ns}&locale=de_DE"
                data = http_get_json(url, headers)
                raw_name = data.get("name")
                if isinstance(raw_name, dict):
                    name = raw_name.get("de_DE") or raw_name.get("en_US") or name
                elif isinstance(raw_name, str) and raw_name:
                    name = raw_name
                sell_price = int(data.get("sell_price", 0))
                quality = data.get("quality", {}).get("type", "COMMON")
                break
            except Exception:
                continue
        cur.execute(
            "INSERT OR REPLACE INTO item_meta(item_id, name, sell_price, quality) VALUES (?, ?, ?, ?)",
            (iid, name, sell_price, quality),
        )
    conn.commit()


def sync_and_prune(conn: sqlite3.Connection, token: str) -> int:
    auctions = fetch_auctions(token)
    now_local = datetime.now().astimezone()
    now_utc_ts = int(datetime.now(timezone.utc).timestamp())

    cur = conn.cursor()
    prev_map = {
        row[0]: (row[1], row[2], row[3], row[4])
        for row in cur.execute(
            "SELECT auction_id, item_id, unit_price, quantity, time_left FROM raw_last_snapshot"
        )
    }

    curr_map = {}
    per_item_prices = defaultdict(list)
    per_item_qty = defaultdict(int)
    per_item_count = defaultdict(int)

    for a in auctions:
        aid = a.get("id")
        item_id = a.get("item", {}).get("id")
        qty = int(a.get("quantity", 1))
        time_left = a.get("time_left", "LONG")

        if "unit_price" in a and a["unit_price"]:
            unit_price = int(a["unit_price"])
        elif a.get("buyout"):
            unit_price = int(a["buyout"]) // max(qty, 1)
        else:
            continue

        if not aid or not item_id or unit_price <= 0:
            continue

        curr_map[aid] = (item_id, unit_price, qty, time_left)
        per_item_prices[item_id].extend([unit_price] * min(qty, 200))
        per_item_qty[item_id] += qty
        per_item_count[item_id] += 1

    est_sold = defaultdict(int)
    for aid, (item_id, _price, prev_qty, prev_tl) in prev_map.items():
        if aid in curr_map:
            curr_qty = curr_map[aid][2]
            if curr_qty < prev_qty:
                est_sold[item_id] += prev_qty - curr_qty
        else:
            if prev_tl != "SHORT":
                est_sold[item_id] += prev_qty

    cur.execute(
        "INSERT INTO snapshots(ts_utc, weekday, hour, total_listings) VALUES (?, ?, ?, ?)",
        (now_utc_ts, now_local.weekday(), now_local.hour, len(curr_map)),
    )
    snap_id = cur.lastrowid

    rows = []
    for item_id, prices in per_item_prices.items():
        prices.sort()
        n = len(prices)
        min_u = prices[0]
        p10_u = prices[max(0, int(n * 0.10) - 1)]
        med_u = int(statistics.median(prices))
        rows.append(
            (
                snap_id,
                item_id,
                min_u,
                p10_u,
                med_u,
                per_item_qty[item_id],
                per_item_count[item_id],
                est_sold.get(item_id, 0),
            )
        )

    cur.executemany(
        """
        INSERT INTO item_stats(
            snapshot_id, item_id, min_unit, p10_unit, median_unit,
            total_qty, listing_count, est_sold_since_prev
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        rows,
    )

    cur.execute("DELETE FROM raw_last_snapshot")
    cur.executemany(
        "INSERT INTO raw_last_snapshot VALUES (?, ?, ?, ?, ?)",
        [(aid, v[0], v[1], v[2], v[3]) for aid, v in curr_map.items()],
    )

    # Historie auf 14 Tage begrenzen
    cutoff_ts = now_utc_ts - (KEEP_DAYS * 86400)
    old_ids = [r[0] for r in cur.execute("SELECT id FROM snapshots WHERE ts_utc < ?", (cutoff_ts,))]
    if old_ids:
        ph = ",".join("?" * len(old_ids))
        cur.execute(f"DELETE FROM item_stats WHERE snapshot_id IN ({ph})", old_ids)
        cur.execute(f"DELETE FROM snapshots WHERE id IN ({ph})", old_ids)

    conn.commit()

    # Einmal taeglich Datenbank komprimieren
    if snap_id % 24 == 0:
        conn.execute("VACUUM")

    print(f"[SYNC OK] Snapshot #{snap_id}: {len(curr_map)} Auktionen, {len(rows)} Items.")
    return snap_id


def build_export(conn: sqlite3.Connection, token: str, top_n: int = 60) -> None:
    cur = conn.cursor()
    snaps = cur.execute(
        "SELECT id, ts_utc, weekday, hour, total_listings FROM snapshots ORDER BY id DESC LIMIT 336"
    ).fetchall()

    latest_id, latest_ts, _, _, latest_listings = snaps[0]
    snap_meta = {s[0]: (s[1], s[2], s[3]) for s in snaps}
    snap_ids = list(snap_meta.keys())
    hours_span = max(1.0, (snaps[0][1] - snaps[-1][1]) / 3600.0) if len(snaps) > 1 else 0.0

    latest_rows = cur.execute(
        """
        SELECT item_id, min_unit, p10_unit, median_unit, total_qty, listing_count
        FROM item_stats WHERE snapshot_id = ? AND total_qty >= 4
        """,
        (latest_id,),
    ).fetchall()

    placeholders = ",".join("?" * len(snap_ids))
    hist_rows = cur.execute(
        f"""
        SELECT snapshot_id, item_id, min_unit, p10_unit, median_unit, total_qty, est_sold_since_prev
        FROM item_stats WHERE snapshot_id IN ({placeholders})
        ORDER BY snapshot_id ASC
        """,
        snap_ids,
    ).fetchall()

    hist_by_item = defaultdict(list)
    for r in hist_rows:
        hist_by_item[r[1]].append(r)

    vendor_map = {
        row[0]: (row[1], row[2], row[3])
        for row in cur.execute("SELECT item_id, name, sell_price, quality FROM item_meta")
    }

    is_collecting = len(snaps) < 2
    candidates = []

    for item_id, cur_min, cur_p10, cur_med, cur_qty, cur_cnt in latest_rows:
        h = hist_by_item.get(item_id, [])
        if not h:
            continue

        flags = []
        is_bait = cur_min < cur_p10 * 0.65 and cur_cnt >= 4
        if is_bait:
            flags.append("BAIT")
        buy_price = cur_p10 if is_bait else cur_min
        if buy_price <= 0:
            continue

        name, vendor_price, quality = vendor_map.get(item_id, (f"Item #{item_id}", 0, "COMMON"))

        if is_collecting:
            # Erst-Lauf: Vorschau anhand aktuellem Intraday-Spread (Min vs. Median)
            if cur_med <= buy_price or cur_cnt < 5:
                continue
            target_sell = cur_med
            est_deposit = int(vendor_price * 0.30)
            net_profit = int(target_sell * (1.0 - AH_CUT) - buy_price - est_deposit)
            if net_profit <= 0:
                continue
            net_roi = (net_profit / buy_price) * 100.0
            if net_roi < 10.0:
                continue
            score = min(65.0, net_roi * 0.6 + min(25.0, cur_cnt * 0.8))
            candidates.append(
                {
                    "item_id": item_id,
                    "name": name,
                    "quality": quality,
                    "score": round(score, 1),
                    "buy_now": buy_price,
                    "cur_min": cur_min,
                    "fair_value": cur_med,
                    "target_sell": target_sell,
                    "est_deposit": est_deposit,
                    "net_profit": net_profit,
                    "net_roi_pct": round(net_roi, 1),
                    "daily_sold_est": 0.0,
                    "flip_rate_pct": 0.0,
                    "cur_qty": cur_qty,
                    "rec_qty": max(1, min(10, cur_qty // 4)),
                    "best_buy_window": "Sammelt Daten...",
                    "best_sell_window": "Sammelt Daten...",
                    "flags": flags,
                    "history": [[snap_meta[r[0]][0], r[2], r[4]] for r in h[-48:]],
                    "heatmap": {},
                }
            )
            continue

        if len(h) < 2:
            continue

        hist_medians = [r[4] for r in h]
        fair_value = int(statistics.median(hist_medians))
        if fair_value <= 0:
            continue

        total_sold = sum(r[6] for r in h)
        avg_qty = max(1.0, statistics.mean(r[5] for r in h))
        daily_sold = (total_sold / hours_span) * 24.0
        daily_turnover = min(1.0, daily_sold / avg_qty)

        if cur_qty > avg_qty * 2.0 and cur_qty >= 20:
            flags.append("SELL_WALL")
        if daily_sold < 5.0:
            flags.append("LOW_LIQ")

        # Zeitfenster-Matrix: Wochentag (0..6) x 4h-Block (0,4,8,12,16,20)
        slot_prices = defaultdict(list)
        for r in h:
            _, wd, hr = snap_meta[r[0]]
            block = (hr // 4) * 4
            slot_prices[(wd, block)].append(r[4])

        slot_medians = {k: int(statistics.median(v)) for k, v in slot_prices.items()}
        best_buy_key = min(slot_medians, key=slot_medians.get)
        best_sell_key = max(slot_medians, key=slot_medians.get)

        target_sell = int(min(fair_value * 1.05, slot_medians[best_sell_key]))
        if target_sell <= buy_price:
            continue

        expected_relists = max(0.0, (1.0 / max(daily_turnover, 0.05)) - 1.0)
        est_deposit = int(vendor_price * 0.30 * min(expected_relists, 5.0))
        net_profit = int(target_sell * (1.0 - AH_CUT) - buy_price - est_deposit)
        if net_profit <= 0:
            continue

        if est_deposit > net_profit * 0.40:
            flags.append("HIGH_DEPOSIT")

        net_roi = (net_profit / buy_price) * 100.0
        discount_pct = ((fair_value - buy_price) / fair_value) * 100.0

        if net_roi < 10.0 or daily_sold < 1.5:
            continue

        penalty = 0.0
        if "SELL_WALL" in flags:
            penalty += 12.0
        if "HIGH_DEPOSIT" in flags:
            penalty += 15.0
        if "LOW_LIQ" in flags:
            penalty += 10.0

        score = max(
            1.0,
            min(
                99.0,
                min(35.0, net_roi * 0.65)
                + min(35.0, daily_turnover * 50.0)
                + min(20.0, max(0.0, discount_pct) * 0.65)
                + min(10.0, daily_sold * 0.25)
                - penalty,
            ),
        )

        rec_qty = max(1, int(min(cur_qty * 0.25, max(1.0, daily_sold * 0.35))))
        heatmap = {f"{wd}_{blk}": price for (wd, blk), price in slot_medians.items()}

        candidates.append(
            {
                "item_id": item_id,
                "name": name,
                "quality": quality,
                "score": round(score, 1),
                "buy_now": buy_price,
                "cur_min": cur_min,
                "fair_value": fair_value,
                "target_sell": target_sell,
                "est_deposit": est_deposit,
                "net_profit": net_profit,
                "net_roi_pct": round(net_roi, 1),
                "daily_sold_est": round(daily_sold, 1),
                "flip_rate_pct": round(daily_turnover * 100.0, 1),
                "cur_qty": cur_qty,
                "rec_qty": rec_qty,
                "best_buy_window": f"{WEEKDAYS_DE[best_buy_key[0]]} {best_buy_key[1]:02d}-{best_buy_key[1]+4:02d}h",
                "best_sell_window": f"{WEEKDAYS_DE[best_sell_key[0]]} {best_sell_key[1]:02d}-{best_sell_key[1]+4:02d}h",
                "flags": flags,
                "history": [[snap_meta[r[0]][0], r[2], r[4]] for r in h[-72:]],
                "heatmap": heatmap,
            }
        )

    candidates.sort(key=lambda x: x["score"], reverse=True)
    top = candidates[:top_n]

    # Deutsche Item-Namen und Vendor-Preise fuer Top-Kandidaten nachladen
    fetch_missing_item_meta(conn, token, [c["item_id"] for c in top])
    for c in top:
        row = cur.execute(
            "SELECT name, quality FROM item_meta WHERE item_id = ?", (c["item_id"],)
        ).fetchone()
        if row:
            c["name"] = row[0]
            c["quality"] = row[1]

    export_payload = {
        "market": "EU / Normal (PvE) / Allianz",
        "ruleset_id": RULESET_ID,
        "house_id": HOUSE_ID,
        "status": "collecting" if is_collecting else "ready",
        "updated_utc": latest_ts,
        "snapshots": len(snaps),
        "hours_span": round(hours_span, 1),
        "total_listings": latest_listings,
        "flips": top,
    }

    with open(EXPORT_JSON_PATH, "w", encoding="utf-8") as f:
        json.dump(export_payload, f, ensure_ascii=False, separators=(",", ":"))
    print(f"[EXPORT OK] {len(top)} Empfehlungen nach {EXPORT_JSON_PATH} geschrieben.")


def main() -> None:
    conn = sqlite3.connect(DB_PATH)
    init_db(conn)
    token = get_oauth_token()
    sync_and_prune(conn, token)
    build_export(conn, token)


if __name__ == "__main__":
    main()
