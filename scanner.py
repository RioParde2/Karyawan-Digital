"""
DJIWA CRYPTO LABS - Karyawan Digital (versi 3.5)

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
from datetime import datetime, timedelta, timezone
import urllib.error
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
STRICT_INSIDER_UNKNOWN = True    # jaringan insider yang porsinya tidak bisa dipastikan = REJECT
MAX_ENTRY_CHANGE_PCT = 300      # Sigap tidak membeli koin yang sudah naik >300% dalam 24 jam
MIN_ENTRY_CHANGE_PCT = -30      # ...dan tidak menangkap pisau jatuh (turun >30% dalam 24 jam)
SHADOW_HOURS = 24               # koin yang ditolak diamati sekian jam untuk menguji akurasi Perisai
SHADOW_MAX = 150
MAX_CHECK_ATTEMPTS = 3          # RugCheck gagal 3x untuk satu koin = ditolak demi keamanan
MAX_TURNOVER = 30               # volume 24 jam > 30x likuiditas = REJECT (rawan volume palsu)
MAX_ACTIVITY = 40               # jumlah catatan aktivitas untuk dashboard
MIN_TRADES_FOR_REVIEW = 10      # penilaian strategi dimulai setelah sekian trade selesai
MAX_MISSES = 3                  # data harga hilang 3 putaran berturut-turut = dianggap rug
DEBUG = True                    # cetak contoh data mentah di log (untuk verifikasi)
# ====================================================================

BASE = "https://api.dexscreener.com"


def get_json(url, headers=None):
    h = {"User-Agent": "karyawan-digital/1.0", "Accept": "application/json"}
    if headers:
        h.update(headers)
    req = urllib.request.Request(url, headers=h)
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


def _insider_network_pct(r, nets):
    """Perkirakan % pasokan yang dipegang jaringan insider. None jika tidak bisa dihitung.
    Satuan jumlah token dari RugCheck belum pasti (mentah atau sudah dibagi desimal),
    jadi dicoba keduanya dan diambil yang paling besar (sikap skeptis)."""
    tok = r.get("token") or {}
    supply, dec = tok.get("supply"), tok.get("decimals") or 0
    if not supply:
        return None
    total = sum(n.get("tokenAmount") for n in nets if isinstance(n.get("tokenAmount"), (int, float)) and n.get("tokenAmount") > 0)
    if not total:
        return None
    shares = [x for x in (total / supply * 100, total / (supply / (10 ** dec)) * 100) if 0 <= x <= 100]
    return max(shares) if shares else None


def rc_headers():
    """Kunci API RugCheck (gratis dari FluxRPC) dikirim lewat header X-API-KEY bila tersedia."""
    key = os.environ.get("RUGCHECK_API_KEY", "").strip()
    return {"X-API-KEY": key} if key else None


def analyze_risk(mint):
    """
    Manajer risiko yang skeptis. Kembalikan dict, atau None jika data tidak bisa diambil.
    Hasil: ok (lolos semua aturan), reject (daftar alasan penolakan), risk, facts, reasons.
    """
    try:
        r = get_json(f"{RUGCHECK}/tokens/{mint}/report", rc_headers())
    except urllib.error.HTTPError as e:
        try:
            body = e.read().decode("utf-8", "ignore")[:200].replace("\n", " ")
        except Exception:
            body = ""
        analyze_risk.last_error = f"HTTP {e.code}"
        print(f"RugCheck gagal untuk {mint}: HTTP {e.code} {body}")
        return None
    except Exception as e:
        analyze_risk.last_error = type(e).__name__
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
    net_pct = 0.0
    if nets:
        net_pct = _insider_network_pct(r, nets)
        if net_pct is None:
            if STRICT_INSIDER_UNKNOWN:
                reject.append("jaringan insider terdeteksi RugCheck dan porsinya tidak bisa dipastikan (ditolak demi keamanan)")
            else:
                risk = min(risk + 15, 100)
                reasons.append("jaringan insider terdeteksi RugCheck (porsi tidak terhitung)")
            net_pct = 0.0
        elif net_pct > MAX_INSIDER_PCT:
            reject.append(f"jaringan insider memegang sekitar {net_pct:.1f}% pasokan (batas {MAX_INSIDER_PCT}%)")
        else:
            reasons.append(f"jaringan insider kecil (~{net_pct:.1f}% pasokan)")

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
            "insider": max(insider_pct, twin_pct, net_pct),
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
        "opened_ts": pos["opened_ts"], "closed_ts": time.time(), "tags": pos.get("tags", {}),
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
    for _, sc, _w, p, an in accepted:
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
        chg = (p.get("priceChange") or {}).get("h24")
        if isinstance(chg, (int, float)):
            if chg > MAX_ENTRY_CHANGE_PCT:
                lines.append(f"Lewati {base.get('symbol', '?')}: sudah naik {chg:.0f}% dalam 24 jam (saya tidak mengejar pucuk).")
                continue
            if chg < MIN_ENTRY_CHANGE_PCT:
                lines.append(f"Lewati {base.get('symbol', '?')}: turun {chg:.0f}% dalam 24 jam (saya tidak menangkap pisau jatuh).")
                continue
        liq = (p.get("liquidity") or {}).get("usd") or 0
        vol = (p.get("volume") or {}).get("h24") or 0
        slip = _slip(amount, liq)
        tokens = (amount * (1 - FEE_PCT / 100)) / (price * (1 + slip))
        pf["cash"] -= amount
        pf["positions"][addr] = {
            "symbol": base.get("symbol", "?"), "url": p.get("url", ""),
            "tokens": tokens, "cost": amount, "realized": 0.0,
            "entry_price": amount / tokens, "entry_liq": liq,
            "peak": price, "last_price": price, "tp1_done": False,
            "opened_ts": time.time(), "misses": 0,
            "tags": {
                "age_h": round((time.time() * 1000 - (p.get("pairCreatedAt") or 0)) / 3_600_000, 1),
                "chg24": chg, "turnover": round(vol / liq, 1) if liq else None, "liq": round(liq),
                "risk": an["risk"] if an else None, "security": (an["facts"].get("security") if an else None),
                "score": round(sc, 1),
            },
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
    raw.setdefault("shadow", [])
    return raw


def save_state(state):
    state["seen"] = sorted(set(state["seen"]))[-2000:]
    SEEN_FILE.write_text(json.dumps(state))


# ==================== BELAJAR DARI DATA ====================
def shadow_add(state, p, reasons):
    """Catat koin yang ditolak Perisai untuk diamati: apakah penolakan itu tepat?"""
    sh = state.setdefault("shadow", [])
    base = p["baseToken"]
    try:
        price = float(p["priceUsd"])
    except (TypeError, ValueError, KeyError):
        return
    if any(x["addr"] == base["address"] for x in sh):
        return
    sh.append({"sym": base.get("symbol", "?"), "addr": base["address"], "price": price,
               "ts": time.time(), "why": (reasons[0] if reasons else "")[:60], "done": False})


def shadow_update(state):
    sh = state.setdefault("shadow", [])
    pending = [x for x in sh if not x["done"] and time.time() - x["ts"] >= SHADOW_HOURS * 3600][:60]
    if pending:
        pairs = fetch_best_pairs([("solana", x["addr"]) for x in pending])
        by = {(q.get("baseToken") or {}).get("address"): q for q in pairs}
        for x in pending:
            q = by.get(x["addr"])
            try:
                now = float(q["priceUsd"]) if q and q.get("priceUsd") else None
            except (TypeError, ValueError):
                now = None
            if now is None:
                x["result"], x["chg"] = "hilang", None
            else:
                chg = (now / x["price"] - 1) * 100 if x["price"] else 0
                x["chg"] = round(chg, 1)
                x["result"] = "rontok" if chg <= -50 else "naik" if chg >= 50 else "datar"
            x["done"] = True
    if len(sh) > SHADOW_MAX:
        sh.sort(key=lambda x: x["ts"])
        del sh[: len(sh) - SHADOW_MAX]


def learning_report(pf, state):
    lines = []
    closed = [c for c in pf["closed"] if "pnl" in c]
    if len(closed) >= MIN_TRADES_FOR_REVIEW:
        n = len(closed)
        wins = [c["pct"] for c in closed if c["pnl"] > 0]
        losses = [c["pct"] for c in closed if c["pnl"] <= 0]
        avg_w = sum(wins) / len(wins) if wins else 0
        avg_l = sum(losses) / len(losses) if losses else 0
        exp = sum(c["pnl"] for c in closed) / n
        lines.append(f"Trade: {n} selesai, menang {len(wins)} ({len(wins) / n * 100:.0f}%). Rata-rata menang {avg_w:+.0f}%, "
                     f"rata-rata kalah {avg_l:+.0f}%, hasil rata-rata ${exp:+.2f} per trade.")
        for name, f in (("naik 24j >100% saat dibeli", lambda t: (t.get("chg24") or 0) > 100),
                        ("umur <12 jam saat dibeli", lambda t: (t.get("age_h") if t.get("age_h") is not None else 99) < 12),
                        ("volume >15x likuiditas saat dibeli", lambda t: (t.get("turnover") or 0) > 15)):
            a = [c for c in closed if c.get("tags") and f(c["tags"])]
            b = [c for c in closed if c.get("tags") and not f(c["tags"])]
            if len(a) >= 3 and len(b) >= 3:
                lines.append(f"- {name}: menang {sum(1 for c in a if c['pnl'] > 0)}/{len(a)}, "
                             f"sisanya {sum(1 for c in b if c['pnl'] > 0)}/{len(b)}")
    else:
        lines.append(f"Trade: {len(closed)} selesai (penilaian strategi mulai setelah {MIN_TRADES_FOR_REVIEW} trade).")
    done = [x for x in state.get("shadow", []) if x.get("done")]
    if len(done) >= 5:
        right = sum(1 for x in done if x["result"] in ("rontok", "hilang"))
        miss = sum(1 for x in done if x["result"] == "naik")
        lines.append(f"Akurasi Perisai: dari {len(done)} koin ditolak (diamati {SHADOW_HOURS} jam), {right} rontok/hilang (penolakan tepat), "
                     f"{miss} naik >50% (terlewat), {len(done) - right - miss} datar.")
    else:
        lines.append(f"Akurasi Perisai: baru {len(done)} koin ditolak yang selesai diamati (minimal 5).")
    return "\n".join(lines)


# ==================== DATA UNTUK DASHBOARD ====================
def update_dashboard(state, pf, n_cand, n_stage1, accepted, rejected, sim_lines, pending=0):
    """Tulis ringkasan ke state['dashboard'] dan catatan ke state['activity'] (dibaca index.html)."""
    wib = datetime.now(timezone.utc) + timedelta(hours=7)
    stamp = wib.strftime("%d/%m %H:%M")
    plist, pos_value, floating = [], 0.0, 0.0
    for addr, pos in pf["positions"].items():
        last = pos.get("last_price", pos["entry_price"])
        val = pos["tokens"] * last
        pnl = val + pos["realized"] - pos["cost"]
        pos_value += val
        floating += pnl
        plist.append({"sym": pos["symbol"], "cost": round(pos["cost"], 2), "value": round(val, 2),
                      "pnl": round(pnl, 2), "pct": round(pnl / pos["cost"] * 100, 1) if pos["cost"] else 0,
                      "age_h": round((time.time() - pos["opened_ts"]) / 3600, 1), "tp1": bool(pos.get("tp1_done"))})
    equity = pf["cash"] + pos_value

    act = state.setdefault("activity", [])
    if n_stage1:
        act.append({"t": stamp, "who": "Radar", "txt": f"{n_stage1} lolos saring awal dari {n_cand} koin"})
    for _, _, _, p, _ in accepted:
        act.append({"t": stamp, "who": "Perisai", "txt": f"LOLOS {p['baseToken'].get('symbol', '?')}"})
    for sym, why in rejected:
        act.append({"t": stamp, "who": "Perisai", "txt": f"TOLAK {sym}: " + (why[0] if why else "")[:80]})
    for ln in sim_lines:
        act.append({"t": stamp, "who": "Sigap", "txt": ln[:120]})
    del act[:-MAX_ACTIVITY]

    n_open = len(pf["positions"])
    state["dashboard"] = {
        "updated_at": wib.strftime("%H:%M") + " WIB",
        "pnl_total": round(equity - pf["start"], 2),
        "pnl_floating": round(floating, 2),
        "open_positions_count": f"{n_open}/{MAX_OPEN_POSITIONS}",
        "positions": plist,
        "status_karyawan": {
            "radar": f"Pindai {n_cand} koin, {n_stage1} lolos",
            "perisai": (f"RugCheck gagal: {pending} koin ditunda" if pending else (f"{len(accepted)} lolos, {len(rejected)} ditolak" if (accepted or rejected) else "Menunggu kandidat dari Radar")),
            "sigap": f"Memantau {n_open} posisi" + (f", {len(sim_lines)} aksi" if sim_lines else ""),
            "warta": f"Nilai ${equity:,.0f} ({(equity / pf['start'] - 1) * 100:+.1f}%)",
        },
    }


def main():
    state = load_state()
    seen = set(state["seen"])
    pf = state["portfolio"]
    candidates = fetch_candidates()
    print(f"Kandidat awal: {len(candidates)} | kunci API RugCheck: {'terpasang' if rc_headers() else 'TIDAK ADA'}")
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
    accepted, rejected, checked, rejected_p, pending = [], [], [], [], []
    for score, warns, p in stage1:
        base = p["baseToken"]
        an = analyze_risk(base["address"])
        time.sleep(1.0)
        if an is None:
            att = state.setdefault("attempts", {})
            n_try = att.get(base["address"], 0) + 1
            att[base["address"]] = n_try
            err = getattr(analyze_risk, "last_error", "?")
            sym_ = base.get("symbol", "?")
            if n_try >= MAX_CHECK_ATTEMPTS:
                why_ = [f"RugCheck gagal {n_try}x ({err}), tidak bisa diverifikasi (ditolak demi keamanan)"]
                checked.append(base["address"])
                rejected.append((sym_, why_))
                rejected_p.append((p, why_))
                att.pop(base["address"], None)
            else:
                pending.append((sym_, err, n_try))
            print(f"{sym_}: gagal dicek ({err}), percobaan {n_try}/{MAX_CHECK_ATTEMPTS}")
            continue
        state.get("attempts", {}).pop(base["address"], None)
        checked.append(base["address"])
        liq_ = (p.get("liquidity") or {}).get("usd") or 0
        vol_ = (p.get("volume") or {}).get("h24") or 0
        if liq_ and vol_ / liq_ > MAX_TURNOVER:
            an["ok"] = False
            an["reject"] = list(an["reject"]) + [f"volume {vol_ / liq_:.0f}x likuiditas (batas {MAX_TURNOVER}x), rawan volume palsu"]
        if an["ok"]:
            accepted.append((score * (1 - an["risk"] / 100), score, warns, p, an))
        else:
            rejected.append((base.get("symbol", "?"), an["reject"]))
            rejected_p.append((p, an["reject"]))
    accepted.sort(key=lambda x: x[0], reverse=True)
    accepted = accepted[:MAX_RESULTS]
    print(f"LOLOS: {len(accepted)} | REJECT: {len(rejected)}")
    for sym, why in rejected:
        print(f"  REJECT {sym}: {'; '.join(why)}")

    # Belajar dari data: catat koin yang ditolak, lalu periksa hasil pengamatan lama
    for p_, why_ in rejected_p:
        shadow_add(state, p_, why_)
    shadow_update(state)

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
        if pending:
            say(PERISAI, "RugCheck belum bisa memeriksa: " + ", ".join(f"{s} ({e}, percobaan {n}/{MAX_CHECK_ATTEMPTS})" for s, e, n in pending)
                + f". Saya tunda dan coba lagi; setelah {MAX_CHECK_ATTEMPTS} kali gagal, koinnya saya tolak demi keamanan.")
        # 3. Sigap menjalankan simulasi
        if sim_lines:
            say(SIGAP, "Menjalankan aturan baku (simulasi, uang pura-pura):\n" + "\n".join(sim_lines))
        # 4. Warta menutup dengan ringkasan
        buys = sum(1 for ln in sim_lines if ln.startswith("BELI"))
        sells = sum(1 for ln in sim_lines if ln.startswith("JUAL"))
        summary = portfolio_summary(pf).split("\n", 1)[1]
        extra = ("\n\nRapor belajar:\n" + learning_report(pf, state)) if (manual or daily) else ""
        say(WARTA, f"Laporan akhir putaran ini: {len(stage1)} lolos saring awal, {len(accepted)} lolos Perisai, "
                   f"{buys} beli dan {sells} jual simulasi.\n{summary}{extra}\n"
                   "Bukan saran keuangan, cek sendiri sebelum bertindak.")

    for addr in checked:
        seen.add(addr)
    update_dashboard(state, pf, len(candidates), len(stage1), accepted, rejected, sim_lines, len(pending))
    state["seen"] = list(seen)
    save_state(state)


if __name__ == "__main__":
    main()
