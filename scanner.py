"""
KARYAWAN DIGITAL - Pemindai Peluang Crypto (versi 1)

Tugasnya: mencari koin baru/trending dari DexScreener (gratis, tanpa API key),
menyaring yang berisiko jelas, lalu mengirim kandidat ke Telegram.
Kamu sendiri yang menganalisa dan menentukan entry/exit.
BUKAN saran keuangan. Tidak ada jaminan profit.
"""
import json
import os
import time
import urllib.parse
import urllib.request
from pathlib import Path

# ====== PENGATURAN (silakan diubah) ======
CHAINS = {"solana", "base", "ethereum", "bsc"}  # kosongkan {} untuk semua chain
MIN_LIQUIDITY_USD = 20_000      # likuiditas minimal (makin besar makin aman keluar)
MIN_VOLUME_24H_USD = 50_000     # volume 24 jam minimal
MIN_TXNS_24H = 150              # jumlah transaksi 24 jam minimal
MIN_AGE_HOURS = 3               # abaikan koin yang terlalu baru (rawan jebakan)
MAX_AGE_HOURS = 24 * 7          # abaikan koin yang lebih tua dari 7 hari
MIN_SELL_TO_BUY = 0.10          # jika jual < 10% dari beli -> curiga honeypot
MAX_RESULTS = 5                 # maksimal kandidat per laporan
SEEN_FILE = Path("seen.json")   # riwayat agar tidak melapor koin yang sama
# ==========================================

BASE = "https://api.dexscreener.com"


def get_json(url):
    req = urllib.request.Request(
        url, headers={"User-Agent": "karyawan-digital/1.0", "Accept": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def fetch_candidates():
    """Ambil daftar koin dari profil terbaru & boost terbaru."""
    found = {}
    for path in ("/token-profiles/latest/v1", "/token-boosts/latest/v1"):
        try:
            data = get_json(BASE + path)
        except Exception as e:
            print(f"Gagal ambil {path}: {e}")
            continue
        if isinstance(data, dict):
            data = [data]
        for item in data:
            chain, addr = item.get("chainId"), item.get("tokenAddress")
            if chain and addr and (not CHAINS or chain in CHAINS):
                found[(chain, addr)] = True
    return list(found)


def fetch_best_pairs(candidates):
    """Untuk tiap koin, ambil pasangan (pair) dengan likuiditas terbesar."""
    by_chain = {}
    for chain, addr in candidates:
        by_chain.setdefault(chain, []).append(addr)

    best = []
    for chain, addrs in by_chain.items():
        for i in range(0, len(addrs), 30):  # maksimal 30 alamat per panggilan
            chunk = addrs[i : i + 30]
            url = f"{BASE}/tokens/v1/{chain}/{','.join(chunk)}"
            try:
                pairs = get_json(url)
            except Exception as e:
                print(f"Gagal ambil pair {chain}: {e}")
                continue
            time.sleep(0.3)
            top = {}
            for p in pairs if isinstance(pairs, list) else []:
                addr = (p.get("baseToken") or {}).get("address")
                liq = (p.get("liquidity") or {}).get("usd") or 0
                if addr and liq > top.get(addr, {}).get("_liq", -1):
                    p["_liq"] = liq
                    top[addr] = p
            best.extend(top.values())
    return best


def evaluate(p, now_ms=None):
    """Kembalikan (skor, peringatan) jika lolos saring, atau None jika tidak."""
    now_ms = now_ms or time.time() * 1000
    liq = (p.get("liquidity") or {}).get("usd") or 0
    vol = (p.get("volume") or {}).get("h24") or 0
    tx = (p.get("txns") or {}).get("h24") or {}
    buys, sells = tx.get("buys", 0), tx.get("sells", 0)
    total = buys + sells
    created = p.get("pairCreatedAt")
    if not created:
        return None
    age_h = (now_ms - created) / 3_600_000
    chg = (p.get("priceChange") or {}).get("h24") or 0

    if liq < MIN_LIQUIDITY_USD or vol < MIN_VOLUME_24H_USD or total < MIN_TXNS_24H:
        return None
    if not (MIN_AGE_HOURS <= age_h <= MAX_AGE_HOURS):
        return None
    if buys > 0 and sells < buys * MIN_SELL_TO_BUY:
        return None  # hampir tidak ada yang jual: ciri honeypot

    score = min(vol / liq, 10) * 2 + min(total / 500, 3) + (1 if buys >= sells else 0)

    warnings = []
    if chg > 300:
        warnings.append("sudah naik tinggi 24j (rawan beli di pucuk)")
    if chg < -40:
        warnings.append("sedang turun dalam")
    if liq < 50_000:
        warnings.append("likuiditas tipis")
    return score, warnings


def fmt(p, score, warnings):
    base = p.get("baseToken") or {}
    chain = p.get("chainId")
    addr = base.get("address", "")
    liq = (p.get("liquidity") or {}).get("usd") or 0
    vol = (p.get("volume") or {}).get("h24") or 0
    tx = (p.get("txns") or {}).get("h24") or {}
    chg = (p.get("priceChange") or {}).get("h24") or 0
    age_h = (time.time() * 1000 - p["pairCreatedAt"]) / 3_600_000
    lines = [
        f"{base.get('name', '?')} ({base.get('symbol', '?')}) - {chain}",
        f"Harga: ${p.get('priceUsd', '?')} | 24j: {chg:+.0f}%",
        f"Likuiditas: ${liq:,.0f} | Volume 24j: ${vol:,.0f}",
        f"Transaksi 24j: {tx.get('buys', 0)} beli / {tx.get('sells', 0)} jual",
        f"Umur: {age_h:.0f} jam | Skor: {score:.1f}",
    ]
    if warnings:
        lines.append("Waspada: " + "; ".join(warnings))
    lines.append(f"Chart: {p.get('url', '')}")
    if chain == "solana":
        lines.append(f"Cek keamanan: https://rugcheck.xyz/tokens/{addr}")
    elif chain in ("ethereum", "base", "bsc"):
        lines.append(f"Cek honeypot: https://honeypot.is/?address={addr}")
    return "\n".join(lines)


def send_telegram(text):
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        print("(Telegram belum diatur - hasil dicetak di sini)\n")
        print(text)
        return
    data = urllib.parse.urlencode(
        {"chat_id": chat_id, "text": text[:4000], "disable_web_page_preview": "true"}
    ).encode()
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage", data=data
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        print("Telegram terkirim:", r.status)


def load_seen():
    try:
        return set(json.loads(SEEN_FILE.read_text()))
    except Exception:
        return set()


def main():
    seen = load_seen()
    candidates = fetch_candidates()
    print(f"Kandidat awal: {len(candidates)}")
    pairs = fetch_best_pairs(candidates)

    results = []
    for p in pairs:
        addr = (p.get("baseToken") or {}).get("address")
        if not addr or addr in seen:
            continue
        ev = evaluate(p)
        if ev:
            results.append((ev[0], ev[1], p))

    results.sort(key=lambda x: x[0], reverse=True)
    results = results[:MAX_RESULTS]
    print(f"Lolos saring: {len(results)}")

    if results:
        header = "Laporan karyawan digital (bukan saran keuangan, cek sendiri sebelum entry)\n"
        body = "\n\n".join(fmt(p, s, w) for s, w, p in results)
        send_telegram(header + "\n" + body)
        for _, _, p in results:
            seen.add(p["baseToken"]["address"])

    SEEN_FILE.write_text(json.dumps(sorted(seen)[-2000:]))


if __name__ == "__main__":
    main()
