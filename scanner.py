"""
DJIWA CRYPTO LABS - Karyawan Digital (versi 2.2)

Karyawan 1 - PENCARI: mencari koin baru/trending dari DexScreener (gratis).
Karyawan 2 - PENILAI RISIKO (manajer risiko & satpam): mengecek ulang tiap
             kandidat Solana lewat RugCheck (gratis, tanpa API key) dengan
             aturan disiplin yang ketat dan skeptis. Gagal satu aturan = REJECT.
Karyawan 4 - PELAPOR: mengirim ringkasan ke Telegram.
(Karyawan 3 - EKSEKUTOR kertas: tahap berikutnya.)

Kamu sendiri yang menganalisa dan menentukan entry/exit.
BUKAN saran keuangan. Tidak ada jaminan profit.
"""
import json
import math
import os
import time
import urllib.parse
import urllib.request
from collections import Counter
from pathlib import Path

# ====== PENGATURAN KARYAWAN 1: PENCARI ======
CHAINS = {"solana"}             # penilai risiko otomatis baru mendukung Solana
MIN_LIQUIDITY_USD = 20_000      # likuiditas minimal
MIN_VOLUME_24H_USD = 50_000     # volume 24 jam minimal
MIN_TXNS_24H = 150              # jumlah transaksi 24 jam minimal
MIN_AGE_HOURS = 3               # abaikan koin yang terlalu baru
MAX_AGE_HOURS = 24 * 7          # abaikan koin yang lebih tua dari 7 hari
MIN_SELL_TO_BUY = 0.10          # jual < 10% dari beli -> curiga honeypot
MAX_RESULTS = 5                 # maksimal kandidat per laporan
SEEN_FILE = Path("seen.json")   # riwayat agar tidak melapor koin yang sama

# ====== PENGATURAN KARYAWAN 2: PENILAI RISIKO (aturan disiplin) ======
MIN_LP_LOCKED_PCT = 90          # likuiditas harus terkunci/terbakar minimal sekian %
MAX_SINGLE_WALLET_PCT = 10      # satu dompet pemilik maksimal sekian % pasokan
MIN_SECURITY_SCORE = 90         # skor keamanan kontrak minimal sekian %
MAX_INSIDER_PCT = 5            # insider / dompet kembar: total maksimal sekian % pasokan
MAX_RISK_TO_REPORT = 70         # skor risiko gabungan >= ini juga ditolak
ANALYZE_MAX = 12                # maksimal kandidat yang diperiksa tiap putaran
RUGCHECK = "https://api.rugcheck.xyz/v1"
DEBUG = True                    # cetak contoh data mentah di log (untuk verifikasi)
# ====================================================================

BASE = "https://api.dexscreener.com"


def get_json(url):
    req = urllib.request.Request(
        url, headers={"User-Agent": "karyawan-digital/1.0", "Accept": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


# ==================== KARYAWAN 1: PENCARI ====================
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

    turnover = vol / liq
    # putaran volume wajar diberi nilai; yang berlebihan dicurigai volume palsu
    t_score = min(turnover, 8) * 2 if turnover <= 15 else max(0, 16 - (turnover - 15) * 0.5)
    score = (
        t_score
        + min(math.log10(max(total, 1)) * 1.5, 6)
        + min(liq / 50_000, 3)
        + (1 if buys >= sells else 0)
    )

    warnings = []
    if turnover > 20:
        warnings.append(f"volume {turnover:.0f}x likuiditas (rawan volume palsu)")
    if chg > 300:
        warnings.append("sudah naik tinggi 24j (rawan beli di pucuk)")
    if chg < -40:
        warnings.append("sedang turun dalam")
    if liq < 50_000:
        warnings.append("likuiditas tipis")
    return score, warnings


# ==================== KARYAWAN 2: PENILAI RISIKO ====================
def _pool_addresses(r):
    """Kumpulkan alamat milik pool/AMM agar tidak dihitung sebagai 'dompet pemilik'."""
    pools = set()
    for addr, info in (r.get("knownAccounts") or {}).items():
        kind = str((info or {}).get("type", "")).upper()
        if kind in ("AMM", "POOL", "LIQUIDITY", "VAULT"):
            pools.add(addr)
    for m in r.get("markets") or []:
        for key in ("pubkey", "liquidityA", "liquidityB", "mintLP"):
            if m.get(key):
                pools.add(m[key])
    return pools


def _main_market_lp_locked(r):
    """% LP terkunci/terbakar pada market terbesar. None jika tidak bisa diverifikasi."""
    best_val, best_pct = -1, None
    for m in r.get("markets") or []:
        lp = m.get("lp") or {}
        pct = lp.get("lpLockedPct")
        if pct is None:
            continue
        val = (lp.get("baseUSD") or 0) + (lp.get("quoteUSD") or 0)
        if val > best_val:
            best_val, best_pct = val, pct
    return best_pct


def analyze_risk(mint):
    """
    Manajer risiko yang skeptis. Kembalikan dict, atau None jika data tidak bisa diambil.
    Hasil: ok (lolos semua aturan), reject (daftar alasan penolakan), risk, facts, reasons.
    """
    try:
        r = get_json(f"{RUGCHECK}/tokens/{mint}/report")
    except Exception as e:
        print(f"RugCheck gagal untuk {mint}: {e}")
        return None

    if DEBUG and not getattr(analyze_risk, "_shown", False):
        analyze_risk._shown = True
        print("DEBUG kunci laporan:", sorted(r.keys()))
        print("DEBUG score:", r.get("score"), "| score_normalised:", r.get("score_normalised"))
        print("DEBUG topHolders[0]:", (r.get("topHolders") or [None])[0])
        print("DEBUG markets[0] lp:", ((r.get("markets") or [{}])[0]).get("lp"))
        ka = r.get("knownAccounts") or {}
        print("DEBUG knownAccounts (2 contoh):", list(ka.items())[:2])
        print("DEBUG risks[0]:", (r.get("risks") or [None])[0])
        print("DEBUG insiderNetworks:", (r.get("insiderNetworks") or [None])[:2] if r.get("insiderNetworks") else r.get("insiderNetworks"))
        print("DEBUG graphInsidersDetected:", r.get("graphInsidersDetected"))
        print("DEBUG jumlah topHolders:", len(r.get("topHolders") or []), "| token:", {k: (r.get("token") or {}).get(k) for k in ("supply", "decimals")})

    reject = []
    if r.get("rugged"):
        reject.append("ditandai RUGGED oleh RugCheck")

    # --- skor risiko gabungan (dari daftar risiko RugCheck) ---
    risk, reasons = 0, []
    for item in r.get("risks") or []:
        level = (item.get("level") or "").lower()
        name = item.get("name") or "?"
        if level == "danger":
            risk += 25
            reasons.append(f"BAHAYA: {name}")
        elif level == "warn":
            risk += 10
            reasons.append(f"waspada: {name}")
    risk = min(risk, 100)

    # --- ATURAN 1: likuiditas harus terkunci ---
    lp_locked = _main_market_lp_locked(r)
    if lp_locked is None:
        reject.append("status kunci likuiditas tidak bisa diverifikasi")
    elif lp_locked < MIN_LP_LOCKED_PCT:
        reject.append(f"likuiditas belum terkunci (hanya {lp_locked:.0f}%, minimal {MIN_LP_LOCKED_PCT}%)")

    # --- ATURAN 2: satu dompet maksimal 10% pasokan (pool tidak dihitung) ---
    pools = _pool_addresses(r)
    wallets = []
    for h in r.get("topHolders") or []:
        if h.get("address") in pools or h.get("owner") in pools:
            continue
        wallets.append(h.get("pct") or 0)
    wallets.sort(reverse=True)
    top_wallet = wallets[0] if wallets else None
    if top_wallet is None:
        reject.append("data pemegang token tidak tersedia")
    elif top_wallet > MAX_SINGLE_WALLET_PCT:
        reject.append(f"satu dompet memegang {top_wallet:.1f}% pasokan (batas {MAX_SINGLE_WALLET_PCT}%)")

    # --- ATURAN 4: insider & dompet kembar (maksimal 5% pasokan) ---
    insider_pct = 0.0
    for h in r.get("topHolders") or []:
        if h.get("address") in pools or h.get("owner") in pools:
            continue
        if h.get("insider"):
            insider_pct += h.get("pct") or 0
    if insider_pct > MAX_INSIDER_PCT:
        reject.append(f"dompet insider memegang {insider_pct:.1f}% pasokan (batas {MAX_INSIDER_PCT}%)")

    twin_pct = 0.0
    for val, cnt in Counter(round(w, 2) for w in wallets if w > 0).items():
        if cnt >= 3 and val * cnt > MAX_INSIDER_PCT:
            twin_pct = max(twin_pct, val * cnt)
            reject.append(f"{cnt} dompet berjumlah identik ({val:.2f}% masing-masing, total {val * cnt:.1f}%): ciri dompet kembar")

    nets = r.get("insiderNetworks") or []
    if nets and insider_pct == 0 and twin_pct == 0:
        risk = min(risk + 15, 100)
        reasons.append("jaringan insider terdeteksi RugCheck (porsi tidak terhitung)")

    # --- ATURAN 3: skor keamanan kontrak minimal 90% ---
    sn = r.get("score_normalised")
    security = None
    if isinstance(sn, (int, float)):
        security = max(0, min(100, 100 - sn))  # skor risiko RugCheck makin tinggi makin bahaya
    else:
        security = max(0, 100 - risk)
    if security < MIN_SECURITY_SCORE:
        reject.append(f"skor keamanan {security:.0f}% (minimal {MIN_SECURITY_SCORE}%)")

    if risk >= MAX_RISK_TO_REPORT:
        reject.append(f"skor risiko gabungan {risk}/100")

    label = "RENDAH" if risk < 25 else "SEDANG" if risk < 50 else "TINGGI"
    return {
        "ok": not reject,
        "reject": reject,
        "risk": risk,
        "label": label,
        "reasons": reasons,
        "facts": {
            "lp_locked": lp_locked,
            "holders": r.get("totalHolders"),
            "top_wallet": top_wallet,
            "security": security,
            "insider": max(insider_pct, twin_pct),
        },
    }


# ==================== KARYAWAN 4: PELAPOR ====================
def fmt(p, score, warnings, an):
    base = p.get("baseToken") or {}
    chain = p.get("chainId")
    addr = base.get("address", "")
    liq = (p.get("liquidity") or {}).get("usd") or 0
    vol = (p.get("volume") or {}).get("h24") or 0
    tx = (p.get("txns") or {}).get("h24") or {}
    chg = (p.get("priceChange") or {}).get("h24") or 0
    age_h = (time.time() * 1000 - p["pairCreatedAt"]) / 3_600_000
    f = an["facts"]
    lines = [
        f"{base.get('name', '?')} ({base.get('symbol', '?')}) - {chain}",
        f"Harga: ${p.get('priceUsd', '?')} | 24j: {chg:+.0f}%",
        f"Likuiditas: ${liq:,.0f} | Volume 24j: ${vol:,.0f}",
        f"Transaksi 24j: {tx.get('buys', 0)} beli / {tx.get('sells', 0)} jual",
        f"Umur: {age_h:.0f} jam | Skor peluang: {score:.1f}",
        "Manajer Risiko: LOLOS semua aturan",
        f" - LP terkunci {f['lp_locked']:.0f}% (min {MIN_LP_LOCKED_PCT}%)",
        f" - Dompet terbesar {f['top_wallet']:.1f}% (maks {MAX_SINGLE_WALLET_PCT}%)",
        f" - Skor keamanan {f['security']:.0f}% (min {MIN_SECURITY_SCORE}%)",
        f" - Insider/dompet kembar {f['insider']:.1f}% (maks {MAX_INSIDER_PCT}%)",
    ]
    if f.get("holders") is not None:
        lines.append(f" - Jumlah holder {f['holders']:,}")
    notes = list(warnings) + an["reasons"][:2]
    if notes:
        lines.append("Catatan: " + "; ".join(notes))
    lines.append(f"Chart: {p.get('url', '')}")
    if chain == "solana":
        lines.append(f"Cek sendiri: https://rugcheck.xyz/tokens/{addr}")
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

    # Karyawan 1: saring awal
    stage1 = []
    for p in pairs:
        addr = (p.get("baseToken") or {}).get("address")
        if not addr or addr in seen:
            continue
        ev = evaluate(p)
        if ev:
            stage1.append((ev[0], ev[1], p))
    stage1.sort(key=lambda x: x[0], reverse=True)
    stage1 = stage1[:ANALYZE_MAX]
    print(f"Lolos saring awal (akan dicek manajer risiko): {len(stage1)}")

    # Karyawan 2: manajer risiko (skeptis: tidak bisa diverifikasi = tidak dilaporkan)
    accepted, rejected, checked = [], [], []
    for score, warns, p in stage1:
        base = p["baseToken"]
        an = analyze_risk(base["address"])
        time.sleep(1.0)
        if an is None:
            print(f"{base.get('symbol', '?')}: gagal dicek, dicoba lagi putaran berikutnya")
            continue
        checked.append(base["address"])
        if an["ok"]:
            accepted.append((score * (1 - an["risk"] / 100), score, warns, p, an))
        else:
            rejected.append((base.get("symbol", "?"), an["reject"]))
    accepted.sort(key=lambda x: x[0], reverse=True)
    accepted = accepted[:MAX_RESULTS]
    print(f"LOLOS: {len(accepted)} | REJECT: {len(rejected)}")
    for sym, why in rejected:
        print(f"  REJECT {sym}: {'; '.join(why)}")

    # Karyawan 4: laporan
    if accepted or rejected:
        header = "Laporan Djiwa Crypto Labs (bukan saran keuangan, cek sendiri sebelum entry)\n"
        body = "\n\n".join(fmt(p, sc, w, an) for _, sc, w, p, an in accepted)
        if not accepted:
            body = "Tidak ada kandidat yang lolos Manajer Risiko putaran ini."
        if rejected:
            body += f"\n\nDitolak Manajer Risiko ({len(rejected)}):"
            for sym, why in rejected[:8]:
                body += f"\n- {sym}: " + "; ".join(why[:2])
        send_telegram(header + "\n" + body)

    for addr in checked:
        seen.add(addr)
    SEEN_FILE.write_text(json.dumps(sorted(seen)[-2000:]))


if __name__ == "__main__":
    main()
