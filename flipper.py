#!/usr/bin/env python3
"""
WoW: Forever Cloud Auto-Flipper (EU / Normal PvE / Allianz -> Ruleset 2, House 111)
Keine externen Pakete noetig (Python 3.10+ Standardbibliothek).
Abwaertskompatibel mit bestehenden wow_forever.db Datenbanken.
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
MAX_SALES_GAP_SEC = 7200  # Max. 2 Stunden zwischen Scans fuer Komplettverkauf-Schaetzung
DB_PATH = "wow_forever.db"
EXPORT_JSON_PATH = "flips_export.json"

WEEKDAYS_DE = ["Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"]


def ensure_column(conn: sqlite3.Connection, table: str, column: str, col_def: str) -> None:
    cols = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    if column not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {col_def}")


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts_utc INTEGER NOT NULL,
            weekday INTEGER NOT NULL,
            hour INTEGER NOT NULL,
            total_listings INTEGER NOT NULL,
            sales_version INTEGER DEFAULT 1,
            delta_sec INTEGER DEFAULT 0,
            sales_gap_ok INTEGER DEFAULT 1
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
            p25_unit INTEGER DEFAULT 0,
            bottom_qty INTEGER DEFAULT 0,
            has_random_suffix INTEGER DEFAULT 0,
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
            quality TEXT DEFAULT 'COMMON',
            item_class TEXT DEFAULT '',
            icon_url TEXT DEFAULT ''
        );

        CREATE INDEX IF NOT EXISTS idx_item_stats_item ON item_stats(item_id);
        CREATE INDEX IF NOT EXISTS idx_snapshots_ts ON snapshots(ts_utc);
        """
    )

    # Sichere Migration fuer bestehende Datenbanken (alte Scans bleiben erhalten)
    ensure_column(conn, "snapshots", "sales_version", "INTEGER DEFAULT 1")
    ensure_column(conn, "snapshots", "delta_sec", "INTEGER DEFAULT 0")
    ensure_column(conn, "snapshots", "sales_gap_ok", "INTEGER DEFAULT 1")
    ensure_column(conn, "item_stats", "p25_unit", "INTEGER DEFAULT 0")
    ensure_column(conn, "item_stats", "bottom_qty", "INTEGER DEFAULT 0")
    ensure_column(conn, "item_stats", "has_random_suffix", "INTEGER DEFAULT 0")
    ensure_column(conn, "item_meta", "item_class", "TEXT DEFAULT ''")
    ensure_column(conn, "item_meta", "icon_url", "TEXT DEFAULT ''")
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


def extract_localized(val, fallback: str = "") -> str:
    if isinstance(val, dict):
        return val.get("de_DE") or val.get("en_US") or fallback
    if isinstance(val, str) and val.strip():
        return val.strip()
    return fallback


def percentile_sorted(sorted_vals: list, pct: float) -> int:
    if not sorted_vals:
        return 0
    idx = min(len(sorted_vals) - 1, max(0, int((len(sorted_vals) - 1) * pct)))
    return int(sorted_vals[idx])


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


def fetch_item_icon(base: str, headers: dict, iid: int, media_href: str, hit_ns: str) -> str:
    urls = []
    if media_href:
        urls.append(ensure_namespace(media_href, hit_ns))
    for ns in (
        hit_ns,
        NAMESPACE_STATIC,
        f"static-classic1x-{REGION}",
        f"static-classic-{REGION}",
        f"static-{REGION}",
    ):
        u = f"{base}/data/wow/media/item/{iid}?namespace={ns}"
        if u not in urls:
            urls.append(u)

    for url in urls:
        try:
            mdata = http_get_json(url, headers)
            for asset in mdata.get("assets", []):
                if asset.get("key") == "icon" and asset.get("value"):
                    return str(asset["value"])
        except Exception:
            continue
    return "NONE"


def fetch_missing_item_meta(conn: sqlite3.Connection, token: str, item_ids: list) -> None:
    if not item_ids:
        return
    cur = conn.cursor()
    placeholders = ",".join("?" * len(item_ids))
    complete = {
        row[0]
        for row in cur.execute(
            f"""
            SELECT item_id FROM item_meta
            WHERE item_id IN ({placeholders})
              AND COALESCE(icon_url, '') != ''
              AND COALESCE(item_class, '') != ''
            """,
            item_ids,
        )
    }
    missing = [iid for iid in item_ids if iid not in complete]
    if not missing:
        return

    headers = {"Authorization": f"Bearer {token}"}
    base = f"https://{REGION}.api.blizzard.com"
    namespaces = (
        NAMESPACE_STATIC,
        f"static-classic1x-{REGION}",
        f"static-classic-{REGION}",
        f"static-{REGION}",
    )

    for iid in missing[:65]:
        existing = cur.execute(
            "SELECT name, sell_price, quality, item_class, icon_url FROM item_meta WHERE item_id = ?",
            (iid,),
        ).fetchone()

        name = existing[0] if existing else f"Item #{iid}"
        sell_price = existing[1] if existing else 0
        quality = existing[2] if existing else "COMMON"
        item_class = (existing[3] or "") if existing else ""
        icon_url = (existing[4] or "") if existing else ""
        media_href = ""
        hit_ns = NAMESPACE_STATIC

        for ns in namespaces:
            try:
                url = f"{base}/data/wow/item/{iid}?namespace={ns}&locale=de_DE"
                data = http_get_json(url, headers)
                name = extract_localized(data.get("name"), name)
                sell_price = int(data.get("sell_price", sell_price))
                quality = data.get("quality", {}).get("type", quality) or quality
                item_class = extract_localized(data.get("item_class", {}).get("name"), item_class)
                media_href = data.get("media", {}).get("key", {}).get("href", "")
                hit_ns = ns
                break
            except Exception:
                continue

        if not icon_url:
            icon_url = fetch_item_icon(base, headers, iid, media_href, hit_ns)
        if not item_class:
            item_class = "Sonstiges"

        cur.execute(
            """
            INSERT OR REPLACE INTO item_meta(item_id, name, sell_price, quality, item_class, icon_url)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (iid, name, sell_price, quality, item_class, icon_url),
        )
    conn.commit()


def sync_and_prune(conn: sqlite3.Connection, token: str) -> int:
    auctions = fetch_auctions(token)
    now_local = datetime.now().astimezone()
    now_utc_ts = int(datetime.now(timezone.utc).timestamp())

    cur = conn.cursor()
    prev_snap = cur.execute(
        "SELECT id, ts_utc FROM snapshots ORDER BY ts_utc DESC LIMIT 1"
    ).fetchone()
    prev_snap_ts = prev_snap[1] if prev_snap else 0
    delta_sec = max(0, now_utc_ts - prev_snap_ts) if prev_snap_ts else 0
    gap_ok = 1 if (prev_snap_ts and 0 < delta_sec <= MAX_SALES_GAP_SEC) else 0

    prev_map = {
        row[0]: (row[1], row[2], row[3], row[4])
        for row in cur.execute(
            "SELECT auction_id, item_id, unit_price, quantity, time_left FROM raw_last_snapshot"
        )
    }

    curr_map = {}
    per_item_prices = defaultdict(list)
    per_item_offers = defaultdict(list)
    per_item_qty = defaultdict(int)
    per_item_count = defaultdict(int)
    per_item_suffix = defaultdict(int)

    for a in auctions:
        aid = a.get("id")
        item_obj = a.get("item", {}) or {}
        item_id = item_obj.get("id")
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
        per_item_offers[item_id].append((unit_price, qty))
        per_item_qty[item_id] += qty
        per_item_count[item_id] += 1

        if (
            bool(item_obj.get("rand"))
            or bool(item_obj.get("seed"))
            or bool(item_obj.get("bonus_lists"))
        ):
            per_item_suffix[item_id] = 1

    # Duplikat-Schutz bei unveraendertem Blizzard-Dump
    if prev_map and curr_map == prev_map and prev_snap:
        print("[SYNC SKIP] Blizzard-Daten seit letztem Scan unveraendert.")
        return prev_snap[0]

    # Preisstatistiken vorab berechnen, damit die Verkaufsschaetzung ueberteuerte Abbrueche filtert
    item_summary = {}
    curr_price_cap = {}
    for item_id, prices in per_item_prices.items():
        prices.sort()
        min_u = prices[0]
        p10_u = percentile_sorted(prices, 0.10)
        p25_u = percentile_sorted(prices, 0.25)
        med_u = int(statistics.median(prices))
        floor_cap = min_u * 1.05
        bottom_qty = sum(q for p, q in per_item_offers[item_id] if p <= floor_cap)
        item_summary[item_id] = (min_u, p10_u, p25_u, med_u, bottom_qty)
        curr_price_cap[item_id] = int(max(p25_u, p10_u) * 1.15)

    # Saubere Verkaufszaehlung (v2):
    # 1) Geschrumpfte Stacks = sicherer Teilverkauf (auch nach Luecken)
    # 2) Verschwundene Auktionen = nur wenn Scan-Luecke <= 2h, Restlaufzeit LONG/VERY_LONG
    #    und Stueckpreis plausibel nahe am Marktpreis (<= P25 * 1.15)
    est_sold = defaultdict(int)
    for aid, (item_id, prev_price, prev_qty, prev_tl) in prev_map.items():
        if aid in curr_map:
            curr_qty = curr_map[aid][2]
            if curr_qty < prev_qty:
                est_sold[item_id] += prev_qty - curr_qty
        elif gap_ok == 1 and prev_tl in ("LONG", "VERY_LONG"):
            cap = curr_price_cap.get(item_id)
            if cap and prev_price <= cap:
                est_sold[item_id] += prev_qty

    cur.execute(
        """
        INSERT INTO snapshots(
            ts_utc, weekday, hour, total_listings, sales_version, delta_sec, sales_gap_ok
        ) VALUES (?, ?, ?, ?, 2, ?, ?)
        """,
        (now_utc_ts, now_local.weekday(), now_local.hour, len(curr_map), delta_sec, gap_ok),
    )
    snap_id = cur.lastrowid

    rows = []
    for item_id, (min_u, p10_u, p25_u, med_u, bottom_qty) in item_summary.items():
        rows.append(
            (
                snap_id,
                item_id,
                min_u,
                p10_u,
                p25_u,
                med_u,
                per_item_qty[item_id],
                per_item_count[item_id],
                est_sold.get(item_id, 0),
                bottom_qty,
                per_item_suffix.get(item_id, 0),
            )
        )

    cur.executemany(
        """
        INSERT INTO item_stats(
            snapshot_id, item_id, min_unit, p10_unit, p25_unit, median_unit,
            total_qty, listing_count, est_sold_since_prev, bottom_qty, has_random_suffix
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        rows,
    )

    cur.execute("DELETE FROM raw_last_snapshot")
    cur.executemany(
        "INSERT INTO raw_last_snapshot VALUES (?, ?, ?, ?, ?)",
        [(aid, v[0], v[1], v[2], v[3]) for aid, v in curr_map.items()],
    )

    cutoff_ts = now_utc_ts - (KEEP_DAYS * 86400)
    old_ids = [r[0] for r in cur.execute("SELECT id FROM snapshots WHERE ts_utc < ?", (cutoff_ts,))]
    if old_ids:
        ph = ",".join("?" * len(old_ids))
        cur.execute(f"DELETE FROM item_stats WHERE snapshot_id IN ({ph})", old_ids)
        cur.execute(f"DELETE FROM snapshots WHERE id IN ({ph})", old_ids)

    conn.commit()

    if snap_id % 24 == 0:
        conn.execute("VACUUM")

    gap_note = f"delta={delta_sec}s, gap_ok={gap_ok}"
    print(f"[SYNC OK] Snapshot #{snap_id}: {len(curr_map)} Auktionen, {len(rows)} Items ({gap_note}).")
    return snap_id


def calc_ref_price(p10_u: int, p25_u: int, listing_cnt: int) -> int:
    if listing_cnt >= 10:
        return p10_u
    return p25_u if p25_u > 0 else p10_u


def sat(val: float, k: float) -> float:
    if val <= 0.0:
        return 0.0
    return val / (val + k)


def build_export(conn: sqlite3.Connection, token: str, top_n: int = 120) -> None:
    cur = conn.cursor()
    latest_row = cur.execute(
        "SELECT id, ts_utc, total_listings FROM snapshots ORDER BY ts_utc DESC LIMIT 1"
    ).fetchone()
    if not latest_row:
        return

    latest_id, latest_ts, latest_listings = latest_row
    min_window_ts = latest_ts - (KEEP_DAYS * 86400)

    snaps = cur.execute(
        """
        SELECT id, ts_utc, weekday, hour, total_listings,
               COALESCE(sales_version, 1), COALESCE(sales_gap_ok, 1), COALESCE(delta_sec, 0)
        FROM snapshots
        WHERE ts_utc >= ?
        ORDER BY ts_utc DESC
        """,
        (min_window_ts,),
    ).fetchall()

    snap_meta = {s[0]: (s[1], s[2], s[3], s[5], s[6], s[7]) for s in snaps}
    snap_ids = list(snap_meta.keys())
    hours_span = max(1.0, (snaps[0][1] - snaps[-1][1]) / 3600.0) if len(snaps) > 1 else 0.0

    cutoff_48h = latest_ts - (48 * 3600)
    cutoff_72h = latest_ts - (72 * 3600)
    snaps_in_48h = sum(1 for s in snaps if s[1] >= cutoff_48h)

    # Zeitbasis fuer saubere v2-Verkaufszaehlung vs. aeltere v1-Scans (sanfter Uebergang)
    v2_valid_ids = {s[0] for s in snaps if s[5] >= 2 and s[6] == 1 and s[7] > 0}
    v2_hours = sum(s[7] for s in snaps if s[0] in v2_valid_ids) / 3600.0
    v1_snaps = [s for s in snaps if s[5] < 2]
    v1_valid_ids = {s[0] for s in v1_snaps}
    v1_hours = (
        max(1.0, (v1_snaps[0][1] - v1_snaps[-1][1]) / 3600.0)
        if len(v1_snaps) >= 2
        else 0.0
    )

    latest_rows = cur.execute(
        """
        SELECT item_id, min_unit, p10_unit, COALESCE(p25_unit, 0), median_unit,
               total_qty, listing_count, COALESCE(bottom_qty, 0), COALESCE(has_random_suffix, 0)
        FROM item_stats WHERE snapshot_id = ? AND total_qty >= 4
        """,
        (latest_id,),
    ).fetchall()

    placeholders = ",".join("?" * len(snap_ids))
    hist_rows = cur.execute(
        f"""
        SELECT snapshot_id, item_id, min_unit, p10_unit, COALESCE(p25_unit, 0),
               median_unit, total_qty, listing_count, est_sold_since_prev,
               COALESCE(has_random_suffix, 0)
        FROM item_stats WHERE snapshot_id IN ({placeholders})
        ORDER BY snapshot_id ASC
        """,
        snap_ids,
    ).fetchall()

    hist_by_item = defaultdict(list)
    total_market_sold = 0
    for r in hist_rows:
        hist_by_item[r[1]].append(r)
        total_market_sold += r[8]

    vendor_map = {
        row[0]: (row[1], row[2], row[3], row[4] or "", row[5] or "")
        for row in cur.execute(
            "SELECT item_id, name, sell_price, quality, item_class, icon_url FROM item_meta"
        )
    }

    is_collecting = len(snaps) < 2 or total_market_sold == 0
    candidates = []

    for (
        item_id,
        cur_min,
        cur_p10,
        cur_p25,
        cur_med,
        cur_qty,
        cur_cnt,
        cur_bottom_qty,
        cur_suffix,
    ) in latest_rows:
        h = hist_by_item.get(item_id, [])
        if not h:
            continue

        # Zeitlich sortierte Historie des Items
        h.sort(key=lambda r: snap_meta[r[0]][0])

        flags = []
        is_bait = cur_min < cur_p10 * 0.65 and cur_cnt >= 4
        if is_bait:
            flags.append("BAIT")
        buy_price = cur_p10 if is_bait else cur_min
        if buy_price <= 0:
            continue

        has_suffix = cur_suffix == 1 or any(r[9] == 1 for r in h)
        if has_suffix:
            flags.append("SUFFIX_GEAR")

        name, vendor_price, quality, item_class, icon_url = vendor_map.get(
            item_id, (f"Item #{item_id}", 0, "COMMON", "", "")
        )
        clean_icon = "" if icon_url == "NONE" else icon_url

        # 72h-Verlauf strikt nach echtem Zeitstempel
        h_72h = [r for r in h if snap_meta[r[0]][0] >= cutoff_72h]
        history_export = [
            [snap_meta[r[0]][0], r[2], calc_ref_price(r[3], r[4], r[7]), r[5], r[6]]
            for r in h_72h
        ]

        if is_collecting:
            ref_now = cur_p25 if cur_p25 > 0 else cur_p10
            if ref_now <= buy_price or cur_cnt < 5:
                continue
            target_sell = ref_now
            est_deposit = int(vendor_price * 0.30 * 0.5)
            net_profit = int(target_sell * (1.0 - AH_CUT) - buy_price - est_deposit)
            if net_profit <= 0:
                continue
            net_roi = (net_profit / buy_price) * 100.0
            if net_roi < 10.0:
                continue
            rec_qty = max(1, min(10, cur_qty // 4))
            if not is_bait and cur_bottom_qty > 0:
                rec_qty = min(rec_qty, cur_bottom_qty)
            total_profit_g = (net_profit * rec_qty) / 10000.0
            penalty = 25.0 if has_suffix else 0.0
            raw_score = 30.0 * sat(total_profit_g, 5.0) + 25.0 * sat(net_roi, 20.0) + 10.0 * sat(cur_cnt, 12.0)
            score = max(1.0, min(65.0, raw_score - penalty))
            candidates.append(
                {
                    "item_id": item_id,
                    "name": name,
                    "quality": quality,
                    "item_class": item_class,
                    "icon_url": clean_icon,
                    "ref_label": "P25" if cur_p25 > 0 else "P10",
                    "score": round(score, 1),
                    "buy_now": buy_price,
                    "cur_min": cur_min,
                    "fair_value": ref_now,
                    "target_sell": target_sell,
                    "est_deposit": est_deposit,
                    "net_profit": net_profit,
                    "net_roi_pct": round(net_roi, 1),
                    "daily_sold_est": 0.0,
                    "flip_rate_pct": 0.0,
                    "cur_qty": cur_qty,
                    "bottom_qty": cur_bottom_qty,
                    "rec_qty": rec_qty,
                    "best_buy_window": "Sammelt Daten...",
                    "best_sell_window": "Sammelt Daten...",
                    "flags": flags,
                    "history": history_export,
                    "heatmap": {},
                }
            )
            continue

        if len(h) < 2:
            continue

        avg_cnt = statistics.mean(r[7] for r in h)
        ref_label = "P10" if avg_cnt >= 10 else "P25"
        hist_refs = [calc_ref_price(r[3], r[4], r[7]) for r in h]
        fair_value = int(statistics.median(hist_refs))
        if fair_value <= 0:
            continue

        # 1) Tagesabsatz mit Vorrang fuer saubere v2-Verkaufszaehlung
        sold_v2 = sum(r[8] for r in h if r[0] in v2_valid_ids)
        sold_v1 = sum(r[8] for r in h if r[0] in v1_valid_ids)

        daily_v2 = (sold_v2 / v2_hours) * 24.0 if v2_hours >= 0.5 else None
        # Alte v1-Werte (inkl. MEDIUM-Ablaeufe) auf 35% daempfen, nur fuer die ersten 6h nach Update
        daily_v1_damp = ((sold_v1 / v1_hours) * 24.0 * 0.35) if v1_hours >= 1.0 else None

        if daily_v2 is not None and (v2_hours >= 6.0 or daily_v1_damp is None):
            daily_sold = daily_v2
        elif daily_v2 is not None and daily_v1_damp is not None:
            w = min(1.0, v2_hours / 6.0)
            daily_sold = (w * daily_v2) + ((1.0 - w) * daily_v1_damp)
        elif daily_v1_damp is not None:
            daily_sold = daily_v1_damp
        else:
            daily_sold = 0.0

        avg_qty = max(1.0, statistics.mean(r[6] for r in h))
        daily_turnover = min(1.0, daily_sold / avg_qty)

        if cur_qty > avg_qty * 2.0 and cur_qty >= 20:
            flags.append("SELL_WALL")
        if daily_sold < 5.0:
            flags.append("LOW_LIQ")

        # 2) 48h-Trendpruefung auf echter Zeitstempel-Basis (erst aktiv ab >= 48h Datenbasis)
        refs_recent_48h = [
            calc_ref_price(r[3], r[4], r[7])
            for r in h
            if snap_meta[r[0]][0] >= cutoff_48h
        ]
        refs_older = [
            calc_ref_price(r[3], r[4], r[7])
            for r in h
            if snap_meta[r[0]][0] < cutoff_48h
        ]
        med_recent_48h = int(statistics.median(refs_recent_48h)) if refs_recent_48h else fair_value

        if (
            hours_span >= 48.0
            and snaps_in_48h >= 20
            and len(refs_recent_48h) >= 8
            and len(refs_older) >= 8
        ):
            med_older = statistics.median(refs_older)
            if med_older > 0 and med_recent_48h < med_older * 0.85:
                flags.append("DOWNTREND")
                fair_value = min(fair_value, med_recent_48h)

        # 3) Zeitfenster-Matrix auf Basis des realistischen Referenzpreises (P10/P25)
        slot_prices = defaultdict(list)
        for r in h:
            _, wd, hr, *_ = snap_meta[r[0]]
            block = (hr // 4) * 4
            slot_prices[(wd, block)].append(calc_ref_price(r[3], r[4], r[7]))

        slot_medians = {k: int(statistics.median(v)) for k, v in slot_prices.items()}
        best_buy_key = min(slot_medians, key=slot_medians.get)
        best_sell_key = max(slot_medians, key=slot_medians.get)

        target_sell = int(min(fair_value * 1.05, slot_medians[best_sell_key]))

        # 4) 72h-Rebound-Test: schuetzt vor Einzel-Ausreissern und abgestuerzten Kursen
        refs_72h_sorted = sorted(calc_ref_price(r[3], r[4], r[7]) for r in h_72h)
        if len(refs_72h_sorted) >= 4:
            cap_p85_72h = percentile_sorted(refs_72h_sorted, 0.85)
            target_sell = min(target_sell, cap_p85_72h)

        if "DOWNTREND" in flags:
            target_sell = min(target_sell, int(med_recent_48h * 1.02))

        if hours_span >= 24.0 and len(h_72h) >= 6 and target_sell > 0:
            rebound_threshold = target_sell * 0.95
            touched_blocks = set()
            touched_last_48h = False
            for r in h_72h:
                ts_r, _, hr_r, *_ = snap_meta[r[0]]
                ref_r = calc_ref_price(r[3], r[4], r[7])
                if ref_r >= rebound_threshold:
                    day_key = datetime.fromtimestamp(ts_r).date()
                    touched_blocks.add((day_key, (hr_r // 4) * 4))
                    if ts_r >= cutoff_48h:
                        touched_last_48h = True

            failed_rebound = len(touched_blocks) < 2 or (hours_span >= 48.0 and not touched_last_48h)
            if failed_rebound:
                flags.append("NO_REBOUND")
                cap_p65_72h = percentile_sorted(refs_72h_sorted, 0.65)
                target_sell = min(target_sell, cap_p65_72h)

        if target_sell <= buy_price:
            continue

        # 5) Reale Gebuehren inkl. Mindest-Relist-Risiko (0.25 Relists gegen 0c-Illusion)
        expected_relists = min(5.0, max(0.25, (1.0 / max(daily_turnover, 0.05)) - 1.0))
        est_deposit = int(vendor_price * 0.30 * expected_relists)
        net_profit = int(target_sell * (1.0 - AH_CUT) - buy_price - est_deposit)
        if net_profit <= 0:
            continue

        if est_deposit > net_profit * 0.40:
            flags.append("HIGH_DEPOSIT")

        net_roi = (net_profit / buy_price) * 100.0
        discount_pct = ((fair_value - buy_price) / fair_value) * 100.0

        if net_roi < 10.0 or daily_sold < 1.5:
            continue

        rec_qty = max(1, int(min(cur_qty * 0.25, max(1.0, daily_sold * 0.35))))
        if not is_bait and cur_bottom_qty > 0:
            rec_qty = min(rec_qty, cur_bottom_qty)

        # 6) Weiche Saettigungskurve + Gewichtung des absoluten Goldgewinns
        total_profit_g = (net_profit * rec_qty) / 10000.0
        pos_discount = max(0.0, discount_pct)

        raw_score = (
            30.0 * sat(total_profit_g, 5.0)     # Halbwert bei 5g Gesamtgewinn pro Flip
            + 25.0 * sat(daily_turnover, 0.20)  # Halbwert bei 20% taegl. Flip-Rate
            + 20.0 * sat(net_roi, 20.0)         # Halbwert bei 20% Netto-ROI
            + 15.0 * sat(pos_discount, 15.0)    # Halbwert bei 15% Rabatt zum Fair Value
            + 10.0 * sat(daily_sold, 12.0)      # Halbwert bei 12 Verkaeufen/Tag
        )

        penalty = 0.0
        if "SELL_WALL" in flags:
            penalty += 12.0
        if "HIGH_DEPOSIT" in flags:
            penalty += 15.0
        if "LOW_LIQ" in flags:
            penalty += 10.0
        if "DOWNTREND" in flags:
            penalty += 18.0
        if "NO_REBOUND" in flags:
            penalty += 12.0
        if "SUFFIX_GEAR" in flags:
            penalty += 25.0

        score = max(1.0, min(99.0, raw_score - penalty))
        heatmap = {f"{wd}_{blk}": price for (wd, blk), price in slot_medians.items()}

        candidates.append(
            {
                "item_id": item_id,
                "name": name,
                "quality": quality,
                "item_class": item_class,
                "icon_url": clean_icon,
                "ref_label": ref_label,
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
                "bottom_qty": cur_bottom_qty,
                "rec_qty": rec_qty,
                "best_buy_window": f"{WEEKDAYS_DE[best_buy_key[0]]} {best_buy_key[1]:02d}-{best_buy_key[1]+4:02d}h",
                "best_sell_window": f"{WEEKDAYS_DE[best_sell_key[0]]} {best_sell_key[1]:02d}-{best_sell_key[1]+4:02d}h",
                "flags": flags,
                "history": history_export,
                "heatmap": heatmap,
            }
        )

    candidates.sort(key=lambda x: x["score"], reverse=True)
    top = candidates[:top_n]

    fetch_missing_item_meta(conn, token, [c["item_id"] for c in top])
    for c in top:
        row = cur.execute(
            "SELECT name, quality, item_class, icon_url FROM item_meta WHERE item_id = ?",
            (c["item_id"],),
        ).fetchone()
        if row:
            c["name"] = row[0]
            c["quality"] = row[1]
            c["item_class"] = row[2] or ""
            c["icon_url"] = "" if row[3] == "NONE" else (row[3] or "")

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
    print(f"[EXPORT OK] {len(top)} Empfehlungen nach {EXPORT_JSON_PATH} geschrieben (v2_hours={v2_hours:.1f}).")


def main() -> None:
    conn = sqlite3.connect(DB_PATH)
    init_db(conn)
    token = get_oauth_token()
    sync_and_prune(conn, token)
    build_export(conn, token)


if __name__ == "__main__":
    main()
