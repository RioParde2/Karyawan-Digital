"""
DJIWA CRYPTO LABS - Karyawan Digital (versi 3.2)

Karyawan 1 - DJIWA RADAR (Pencari): mencari koin baru/trending dari DexScreener (gratis).
Karyawan 2 - DJIWA PERISAI (Penilai Risiko, manajer risiko & satpam): mengecek ulang tiap
             kandidat Solana lewat RugCheck (gratis, tanpa API key) dengan
             aturan disiplin yang ketat dan skeptis. Gagal satu aturan = REJECT.
Karyawan 4 - DJIWA WARTA (Penyusun & Pelapor): mengirim ringkasan ke Telegram.
Karyawan 3 - DJIWA SIGAP (Eksekutor SIMULASI): trading uang pura-pura dengan aturan baku.
             Tidak ada dompet, tidak ada uang sungguhan. Data harga asli DexScreener.

Kamu sendiri yang menganalisa dan menentukan entry/exit.
BUKAN saran keuangan. Tidak ada jaminan profit.
"""
import html
import json
import math
import os
import re
import time
from datetime import datetime, timezone
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

# ====== PENGATURAN KARYAWAN 3: EKSEKUTOR SIMULASI (uang pura-pura) ======
PAPER_START_BALANCE = 1000      # modal simulasi (USD)
POSITION_USD = 50               # ukuran tiap posisi (5% modal)
MAX_OPEN_POSITIONS = 5          # maksimal posisi terbuka sekaligus
STOP_LOSS_PCT = 25              # jual semua jika turun 25% dari harga masuk
TP1_GAIN_PCT = 50               # take profit: jual sebagian di +50%
TP1_SELL_FRACTION = 0.5         # ...sebanyak separuh posisi
TRAIL_FROM_PEAK_PCT = 20        # sisa dijual jika turun 20% dari puncak (setelah take profit)
MAX_HOLD_HOURS = 48             # jual jika lewat 48 jam
DAILY_LOSS_LIMIT_PCT = 5        # rugi bersih harian 5% modal -> stop beli sampai besok (UTC)
LIQ_COLLAPSE_PCT = 50           # jual jika likuiditas anjlok lebih dari 50% (tanda rug)
FEE_PCT = 1.0                   # biaya per transaksi (fee DEX + biaya lain), konservatif
MAX_MISSES = 3                  # data harga hilang 3 putaran berturut-turut = dianggap rug
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


def stage1_reason(p, now_ms=None):
    """Kembalikan alasan koin gagal saring awal, atau None jika lolos."""
    now_ms = now_ms or time.time() * 1000
    liq = (p.get("liquidity") or {}).get("usd") or 0
    vol = (p.get("volume") or {}).get("h24") or 0
    tx = (p.get("txns") or {}).get("h24") or {}
    buys, sells = tx.get("buys", 0), tx.get("sells", 0)
    created = p.get("pairCreatedAt")
    if not created:
        return "data umur kosong"
    age_h = (now_ms - created) / 3_600_000
    if liq < MIN_LIQUIDITY_USD:
        return "likuiditas kecil"
    if vol < MIN_VOLUME_24H_USD:
        return "volume kecil"
    if buys + sells < MIN_TXNS_24H:
        return "transaksi sedikit"
    if age_h < MIN_AGE_HOURS:
        return "terlalu baru"
    if age_h > MAX_AGE_HOURS:
        return "terlalu lama"
    if buys > 0 and sells < buys * MIN_SELL_TO_BUY:
        return "ciri honeypot"
    return None


def evaluate(p, now_ms=None):
    """Kembalikan (skor, peringatan) jika lolos saring, atau None jika tidak."""
    now_ms = now_ms or time.time() * 1000
    if stage1_reason(p, now_ms):
        return None
    liq = (p.get("liquidity") or {}).get("usd") or 0
    vol = (p.get("volume") or {}).get("h24") or 0
    tx = (p.get("txns") or {}).get("h24") or {}
    buys, sells = tx.get("buys", 0), tx.get("sells", 0)
    total = buys + sells
    chg = (p.get("priceChange") or {}).get("h24") or 0

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


# ==================== NAMA KARYAWAN & OBROLAN TIM ====================
RADAR = "🛰️ Djiwa Radar"
PERISAI = "🛡️ Djiwa Perisai"
SIGAP = "⚙️ Djiwa Sigap"
WARTA = "📰 Djiwa Warta"


def say(who, body):
    """Kirim satu pesan sebagai karyawan tertentu, dengan jeda agar terbaca seperti obrolan tim."""
    send_telegram(f"<b>{who}</b>\n{html.escape(body)}", use_html=True)
    time.sleep(1.5)


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


def send_telegram(text, use_html=False):
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        print("(Telegram belum diatur - hasil dicetak di sini)\n")
        print(re.sub(r"</?b>", "", html.unescape(text)) if use_html else text)
        return
    payload = {"chat_id": chat_id, "text": text[:4000], "disable_web_page_preview": "true"}
    if use_html:
        payload["parse_mode"] = "HTML"
    data = urllib.parse.urlencode(payload).encode()
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage", data=data
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        print("Telegram terkirim:", r.status)


# ==================== KARYAWAN 3: EKSEKUTOR (SIMULASI) ====================
def _today():
    return datetime.fromtimestamp(time.time(), timezone.utc).strftime("%Y-%m-%d")


def _slip(usd, liq):
    """Perkiraan geseran harga di pool: kira-kira 2 x ukuran transaksi / likuiditas."""
    if not liq or liq <= 0:
        return 0.5
    return min(2 * usd / liq, 0.5)


def paper_sell(pf, pos, tokens, price, liq):
    gross = tokens * price
    proceeds = gross * (1 - _slip(gross, liq)) * (1 - FEE_PCT / 100)
    pf["cash"] += proceeds
    pos["tokens"] -= tokens
    pos["realized"] += proceeds
    return proceeds


def paper_close(pf, addr, pos, reason):
    pnl = pos["realized"] - pos["cost"]
    pct = pnl / pos["cost"] * 100 if pos["cost"] else 0
    pf["closed"].append({
        "symbol": pos["symbol"], "addr": addr, "cost": round(pos["cost"], 2),
        "proceeds": round(pos["realized"], 2), "pnl": round(pnl, 2),
        "pct": round(pct, 1), "reason": reason, "day": _today(),
        "opened_ts": pos["opened_ts"], "closed_ts": time.time(),
    })
    pf["closed"] = pf["closed"][-300:]
    del pf["positions"][addr]
    return (f"JUAL {pos['symbol']} ({reason}): modal ${pos['cost']:.2f}, "
            f"hasil ${pos['realized']:.2f} = {pnl:+.2f} USD ({pct:+.0f}%)")


def paper_exits(pf):
    """Periksa semua posisi terbuka dan jual sesuai aturan baku."""
    positions = pf["positions"]
    if not positions:
        return []
    pairs = fetch_best_pairs([("solana", a) for a in positions])
    by_addr = {(p.get("baseToken") or {}).get("address"): p for p in pairs}
    lines = []
    for addr, pos in list(positions.items()):
        p = by_addr.get(addr)
        try:
            price = float(p["priceUsd"]) if p and p.get("priceUsd") else None
        except (TypeError, ValueError):
            price = None
        liq = ((p or {}).get("liquidity") or {}).get("usd") or 0

        if price is None:
            pos["misses"] = pos.get("misses", 0) + 1
            if pos["misses"] >= MAX_MISSES:
                lines.append(paper_close(pf, addr, pos, "data harga hilang, dianggap rug (hasil 0)"))
            continue

        pos["misses"] = 0
        pos["last_price"] = price
        pos["peak"] = max(pos.get("peak", price), price)
        age_h = (time.time() - pos["opened_ts"]) / 3600
        entry = pos["entry_price"]

        reason = None
        if liq and pos.get("entry_liq") and liq < pos["entry_liq"] * (1 - LIQ_COLLAPSE_PCT / 100):
            reason = f"likuiditas anjlok >{LIQ_COLLAPSE_PCT}%, tanda rug"
        elif price <= entry * (1 - STOP_LOSS_PCT / 100):
            reason = f"stop-loss -{STOP_LOSS_PCT}%"
        elif pos.get("tp1_done") and price <= pos["peak"] * (1 - TRAIL_FROM_PEAK_PCT / 100):
            reason = f"trailing stop {TRAIL_FROM_PEAK_PCT}% dari puncak"
        elif age_h >= MAX_HOLD_HOURS:
            reason = f"batas waktu {MAX_HOLD_HOURS} jam"

        if reason:
            paper_sell(pf, pos, pos["tokens"], price, liq)
            lines.append(paper_close(pf, addr, pos, reason))
        elif not pos.get("tp1_done") and price >= entry * (1 + TP1_GAIN_PCT / 100):
            got = paper_sell(pf, pos, pos["tokens"] * TP1_SELL_FRACTION, price, liq)
            pos["tp1_done"] = True
            pos["peak"] = price
            lines.append(f"JUAL SEPARUH {pos['symbol']} (take profit +{TP1_GAIN_PCT}%): hasil ${got:.2f}, sisa ditahan dengan trailing stop")
    return lines


def daily_halted(pf):
    today = _today()
    realized = sum(c["pnl"] for c in pf["closed"] if c.get("day") == today)
    return realized <= -DAILY_LOSS_LIMIT_PCT / 100 * pf["start"]


def paper_enter(pf, accepted):
    """Beli (simulasi) kandidat yang lolos Manajer Risiko, sesuai batas posisi dan kas."""
    if not accepted:
        return []
    if daily_halted(pf):
        return [f"Batas rugi harian {DAILY_LOSS_LIMIT_PCT}% tercapai: tidak membuka posisi baru sampai besok (UTC)."]
    lines = []
    for _, _, _, p, _ in accepted:
        base = p["baseToken"]
        addr = base["address"]
        if addr in pf["positions"]:
            continue
        if len(pf["positions"]) >= MAX_OPEN_POSITIONS:
            lines.append(f"Lewati {base.get('symbol', '?')}: sudah {MAX_OPEN_POSITIONS} posisi terbuka.")
            break
        amount = min(POSITION_USD, pf["cash"])
        if amount < POSITION_USD:
            lines.append("Kas simulasi tidak cukup untuk posisi baru.")
            break
        try:
            price = float(p["priceUsd"])
        except (TypeError, ValueError, KeyError):
            continue
        liq = (p.get("liquidity") or {}).get("usd") or 0
        slip = _slip(amount, liq)
        tokens = (amount * (1 - FEE_PCT / 100)) / (price * (1 + slip))
        pf["cash"] -= amount
        pf["positions"][addr] = {
            "symbol": base.get("symbol", "?"), "url": p.get("url", ""),
            "tokens": tokens, "cost": amount, "realized": 0.0,
            "entry_price": amount / tokens, "entry_liq": liq,
            "peak": price, "last_price": price, "tp1_done": False,
            "opened_ts": time.time(), "misses": 0,
        }
        lines.append(
            f"BELI {base.get('symbol', '?')}: ${amount:.0f} @ ${price:.8f} (geseran ~{slip * 100:.1f}% + fee {FEE_PCT}%) | "
            f"stop-loss -{STOP_LOSS_PCT}% | take profit separuh di +{TP1_GAIN_PCT}% | maks {MAX_HOLD_HOURS} jam"
        )
    return lines


def portfolio_summary(pf):
    pos_value = sum(p["tokens"] * p.get("last_price", p["entry_price"]) for p in pf["positions"].values())
    equity = pf["cash"] + pos_value
    closed = pf["closed"]
    wins = sum(1 for c in closed if c["pnl"] > 0)
    realized = sum(c["pnl"] for c in closed)
    return (
        "[SIMULASI] Ringkasan uang pura-pura\n"
        f"Nilai total (perkiraan): ${equity:,.2f} ({(equity / pf['start'] - 1) * 100:+.1f}% dari ${pf['start']:,.0f})\n"
        f"Kas: ${pf['cash']:,.2f} | posisi terbuka: {len(pf['positions'])}\n"
        f"Trade selesai: {len(closed)} (menang {wins}) | untung/rugi terealisasi: {realized:+.2f} USD"
    )


# ==================== PENYIMPANAN STATUS ====================
def load_state():
    """Status disimpan di seen.json: riwayat koin yang sudah dicek + portofolio simulasi."""
    try:
        raw = json.loads(SEEN_FILE.read_text())
    except Exception:
        raw = {}
    if isinstance(raw, list):  # format lama: hanya daftar koin
        raw = {"seen": raw}
    raw.setdefault("seen", [])
    pf = raw.setdefault("portfolio", {})
    pf.setdefault("start", PAPER_START_BALANCE)
    pf.setdefault("cash", pf["start"])
    pf.setdefault("positions", {})
    pf.setdefault("closed", [])
    return raw


def save_state(state):
    state["seen"] = sorted(set(state["seen"]))[-2000:]
    SEEN_FILE.write_text(json.dumps(state))


def main():
    state = load_state()
    seen = set(state["seen"])
    pf = state["portfolio"]
    candidates = fetch_candidates()
    print(f"Kandidat awal: {len(candidates)}")
    pairs = fetch_best_pairs(candidates)
    print(f"Data pair ditemukan: {len(pairs)}")

    # Karyawan 1: saring awal (sambil menghitung alasan gagal)
    funnel = Counter()
    stage1 = []
    for p in pairs:
        addr = (p.get("baseToken") or {}).get("address")
        if not addr:
            continue
        if addr in seen:
            funnel["sudah dilaporkan"] += 1
            continue
        why = stage1_reason(p)
        if why:
            funnel[why] += 1
            continue
        ev = evaluate(p)
        if ev:
            stage1.append((ev[0], ev[1], p))
    funnel_text = ", ".join(f"{k} {v}" for k, v in funnel.most_common()) or "-"
    print(f"Alasan tersaring: {funnel_text}")
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

    # Karyawan 3: eksekutor simulasi (jual dulu, baru beli)
    sim_lines = paper_exits(pf)
    sim_lines += paper_enter(pf, accepted)
    for ln in sim_lines:
        print("  SIMULASI:", ln)

    # Karyawan 4: obrolan tim di Telegram (Radar -> Perisai -> Sigap -> Warta)
    manual = os.environ.get("GITHUB_EVENT_NAME", "workflow_dispatch") == "workflow_dispatch"
    daily = state.get("last_daily") != _today()  # kabar harian: sekali per hari (UTC)
    activity = bool(stage1 or accepted or rejected or sim_lines)
    if activity or manual or daily:
        state["last_daily"] = _today()
        # 1. Radar melapor temuan
        if stage1:
            names = ", ".join(p["baseToken"].get("symbol", "?") for _, _, p in stage1)
            say(RADAR, f"Saya pindai {len(candidates)} koin Solana. {len(stage1)} lolos saring awal: {names}. "
                       "Perisai, tolong periksa ulang dengan ketat.")
        elif manual or daily:
            say(RADAR, f"Saya pindai {len(candidates)} koin Solana, belum ada yang lolos saring awal. "
                       f"Tersaring: {funnel_text}.")
        # 2. Perisai memeriksa
        if accepted or rejected:
            total = len(accepted) + len(rejected)
            msg = f"Saya periksa {total} kandidat: {len(accepted)} lolos, {len(rejected)} ditolak."
            if rejected:
                msg += "\n\nDitolak:"
                for sym, why in rejected[:8]:
                    msg += f"\n- {sym}: " + "; ".join(why[:2])
            msg += ("\n\nYang lolos saya teruskan ke Sigap." if accepted
                    else "\n\nTidak ada yang lolos, Sigap tidak perlu bergerak.")
            say(PERISAI, msg)
            for _, sc, w, p, an in accepted:
                say(PERISAI, fmt(p, sc, w, an))
        # 3. Sigap menjalankan simulasi
        if sim_lines:
            say(SIGAP, "Menjalankan aturan baku (simulasi, uang pura-pura):\n" + "\n".join(sim_lines))
        # 4. Warta menutup dengan ringkasan
        buys = sum(1 for ln in sim_lines if ln.startswith("BELI"))
        sells = sum(1 for ln in sim_lines if ln.startswith("JUAL"))
        summary = portfolio_summary(pf).split("\n", 1)[1]
        say(WARTA, f"Laporan akhir putaran ini: {len(stage1)} lolos saring awal, {len(accepted)} lolos Perisai, "
                   f"{buys} beli dan {sells} jual simulasi.\n{summary}\nBukan saran keuangan, cek sendiri sebelum bertindak.")

    for addr in checked:
        seen.add(addr)
    state["seen"] = list(seen)
    save_state(state)


if __name__ == "__main__":
    main()
