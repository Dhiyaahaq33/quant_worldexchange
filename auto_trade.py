import asyncio
import ccxt.pro as ccxtpro
import aiohttp
from telebot import TeleBot
import pandas as pd
import time
import csv
import os
from datetime import datetime
from collections import deque
import os
from dotenv import load_dotenv

load_dotenv("DATA.env")

# ================== SETTING ==================
TELEGRAM_TOKEN = os.getenv("TOKEN_BNCMEXC")
CHAT_ID = os.getenv("CHAT_ID")

if not TELEGRAM_TOKEN or not CHAT_ID:
    raise ValueError(
        "TELEGRAM_TOKEN / CHAT_ID belum diset. "
        "Set env var dulu sebelum menjalankan bot."
    )

SYMBOL         = "SOL/USDT"
RISK_USD       = 1000
FEE_MAKER      = 0.0000  # 0.00% maker
FEE_TAKER      = 0.0005  # 0.05% taker (hanya SL Plan B emergency)

SL_PCT         = 0.0010  # -0.10% dari entry
BODY_THRESHOLD = 0.40    # green candle body >= 40% range
FLOW_THRESHOLD = 0.60    # buyer dominance >= 60%
FLOW_WINDOW    = 30      # detik
WAIT_WINDOW    = 3       # candle window setelah BB confirmed
CANDLE_BUFFER  = 40      # jumlah candle yang disimpan di buffer

# SL Plan B trigger: jika harga turun sekian di bawah SL limit, switch ke taker
SL_PLAN_B_OFFSET = 0.01
MIN_RR           = 1.5   # minimum RR gross sebelum limit entry di-set

# Limit entry expire: berapa candle sebelum dibatalkan
LIMIT_EXPIRE_CANDLES = 1

JOURNAL_FILE = "journal_bb_maker.csv"

bot = TeleBot(TELEGRAM_TOKEN)

flow = {
    'buy_vol': 0.0, 'sell_vol': 0.0,
    'start_time': time.time(),
    'last_ratio': 0.0,
    'last_update': 0.0
}

# ── Shared candle buffers (diisi oleh kline coroutine) ────────
# Format per entry: [ts, open, high, low, close, volume]
candles_mexc = deque(maxlen=CANDLE_BUFFER)
candles_bnb  = deque(maxlen=CANDLE_BUFFER)

# Lock agar scanner tidak baca buffer saat sedang diupdate
lock_mexc = asyncio.Lock()
lock_bnb  = asyncio.Lock()

virtual_pos     = None
current_balance = RISK_USD
last_miss_logged_idx = None

bb_setup = {
    'touched': False,
    'confirmed': False,
    'confirm_candle_idx': None,
    'attempts': 0,
    'last_candle_idx': None
}

pending_limit = {
    'active': False,
    'price': None,
    'set_at_idx': None,
    'last_candle_idx': None,
    'attempts': 0,
    'bb_high_at_set': None,
    'body_ratio_at_set': None,
    'bb_low_at_set': None,
    'flow_at_set': 0.0,
    'set_candle_ts': None
}

sl_state = {
    'plan':           'A',
    'sl_price':       None,
    'plan_b_trigger': None
}

tp_state = {
    'tp_price': None,
    'tp_entry': None
}

# ================== JOURNAL ==================
def init_journal():
    if not os.path.exists(JOURNAL_FILE):
        with open(JOURNAL_FILE, 'w', newline='') as f:
            writer = csv.writer(f)
            writer.writerow([
                'timestamp', 'type', 'symbol', 'price',
                'size_sol', 'sl', 'tp_at_entry', 'tp_at_exit',
                'pnl_gross', 'pnl_net',
                'flow_ratio', 'body_ratio',
                'bb_low', 'bb_high',
                'rr_gross', 'rr_net',
                # [FIX BUG 9] Tambah kolom RR saat entry vs saat exit
                'rr_entry_gross', 'rr_entry_net',
                'miss_green', 'miss_flow',
                'retrace_candle', 'limit_price',
                'sl_plan', 'fill_type'
            ])

# [FIX BUG 9] Tambah parameter rr_entry_gross dan rr_entry_net
def log_trade(type_, price, size=None, sl=None,
              tp_at_entry=None, tp_at_exit=None,
              pnl_gross=None, pnl_net=None,
              body_ratio=None, bb_low=None, bb_high=None,
              rr_gross=None, rr_net=None,
              rr_entry_gross=None, rr_entry_net=None,
              miss_green=False, miss_flow=False,
              retrace_candle=None, limit_price=None,
              sl_plan=None, fill_type=None,
              flow_ratio=None):
    with open(JOURNAL_FILE, 'a', newline='') as f:
        writer = csv.writer(f)
        writer.writerow([
            datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            type_, SYMBOL, f"{price:.4f}",
            f"{size:.4f}"          if size           is not None else '',
            f"{sl:.4f}"            if sl              is not None else '',
            f"{tp_at_entry:.4f}"   if tp_at_entry     is not None else '',
            f"{tp_at_exit:.4f}"    if tp_at_exit      is not None else '',
            f"{pnl_gross:.2f}"     if pnl_gross       is not None else '',
            f"{pnl_net:.2f}"       if pnl_net         is not None else '',
            f"{(flow_ratio if flow_ratio is not None else flow['last_ratio'])*100:.1f}%",
            f"{body_ratio:.2f}"    if body_ratio       is not None else '',
            f"{bb_low:.4f}"        if bb_low           is not None else '',
            f"{bb_high:.4f}"       if bb_high          is not None else '',
            f"1:{rr_gross:.2f}"    if rr_gross         is not None else '',
            f"1:{rr_net:.2f}"      if rr_net           is not None else '',
            # [FIX BUG 9] Log RR saat entry (hanya terisi di baris TP/SL)
            f"1:{rr_entry_gross:.2f}" if rr_entry_gross is not None else '',
            f"1:{rr_entry_net:.2f}"   if rr_entry_net  is not None else '',
            miss_green, miss_flow,
            retrace_candle         if retrace_candle   is not None else '',
            f"{limit_price:.4f}"   if limit_price      is not None else '',
            sl_plan                if sl_plan           is not None else '',
            fill_type              if fill_type         is not None else ''
        ])

# ================== HELPER ==================
def _mark(ok: bool) -> str:
    return "✅" if ok else "❌"

def reset_bb_setup():
    global bb_setup
    bb_setup = {
        'touched': False,
        'confirmed': False,
        'confirm_candle_idx': None,
        'attempts': 0,
        'last_candle_idx': None
    }
    print("🔄 BB setup reset")

def reset_pending_limit():
    global pending_limit
    pending_limit = {
        'active': False,
        'price': None,
        'set_at_idx': None,
        'last_candle_idx': None,
        'attempts': 0,
        'bb_high_at_set': None,
        'body_ratio_at_set': None,
        'bb_low_at_set': None,
        'flow_at_set': 0.0,
        'set_candle_ts': None
    }
    print("🔄 Pending limit entry reset")

def reset_sl_state():
    global sl_state
    sl_state = {'plan': 'A', 'sl_price': None, 'plan_b_trigger': None}

def reset_tp_state():
    global tp_state
    tp_state = {'tp_price': None, 'tp_entry': None}

def make_session():
    resolver = aiohttp.ThreadedResolver()
    connector = aiohttp.TCPConnector(resolver=resolver)
    return aiohttp.ClientSession(connector=connector)

def build_df(candle_deque):
    """Convert deque buffer ke DataFrame dengan BB20."""
    df = pd.DataFrame(
        list(candle_deque),
        columns=['ts', 'open', 'high', 'low', 'close', 'volume']
    )
    df['bb_mid']  = df['close'].rolling(20).mean()
    df['std']     = df['close'].rolling(20).std()
    df['bb_low']  = df['bb_mid'] - 2 * df['std']
    df['bb_high'] = df['bb_mid'] + 2 * df['std']
    return df

def calc_rr(entry, sl, tp, fee_entry=FEE_MAKER, fee_exit=FEE_MAKER):
    gross_risk   = entry - sl
    gross_reward = tp - entry
    net_risk     = gross_risk   + entry * fee_entry + entry * fee_exit
    net_reward   = gross_reward - entry * fee_entry - entry * fee_exit
    rr_gross = gross_reward / gross_risk if gross_risk > 0 else 0
    rr_net   = net_reward   / net_risk   if net_risk   > 0 else 0
    be_net   = 1 / (1 + rr_net) * 100   if rr_net  > 0 else 100
    return rr_gross, rr_net, be_net

# ================== TELEGRAM ==================
async def send(msg):
    try:
        bot.send_message(CHAT_ID, msg, parse_mode='HTML')
        print(f"📨 {msg[:80]}...")
    except Exception as e:
        print(f"Telegram error: {e}")

# ================== DEBUG PRINTER ==================
async def debug_printer():
    while True:
        await asyncio.sleep(10)
        pos_info = (
            f"Entry {virtual_pos['entry']:.4f} | "
            f"TP~{tp_state['tp_price']:.4f} | "
            f"SL Plan {sl_state['plan']} @ {sl_state['sl_price']:.4f}"
            if virtual_pos else "Kosong"
        )
        bb_info = (
            f"touched={bb_setup['touched']} | "
            f"confirmed={bb_setup['confirmed']} | "
            f"attempts={bb_setup['attempts']}/{WAIT_WINDOW}"
        )
        limit_info = (
            f"Limit@{pending_limit['price']:.4f} | "
            f"candle {pending_limit['attempts']}/{LIMIT_EXPIRE_CANDLES}"
            if pending_limit['active'] else "Tidak ada"
        )
        buf_info = f"MEXC={len(candles_mexc)} BNB={len(candles_bnb)} candles"
        print(
            f"[{datetime.now().strftime('%H:%M:%S')}] "
            f"Flow: {flow['last_ratio']*100:.1f}% | "
            f"Pos: {pos_info} | BB: {bb_info} | "
            f"Limit: {limit_info} | {buf_info} | "
            f"Balance: ${current_balance:.2f}"
        )

# ================== KLINE STREAM — MEXC ==================
async def kline_mexc():
    ex = ccxtpro.mexc({
        'enableRateLimit': True,
        'session': make_session(),
        'options': {
            'defaultType': 'spot',
            'fetchMarkets': ['spot'],
        }
    })
    print("✅ MEXC kline WebSocket connecting...")
    try:
        seed = await ex.fetch_ohlcv(SYMBOL, timeframe='1m', limit=CANDLE_BUFFER)
        async with lock_mexc:
            for c in seed:
                candles_mexc.append(c)
        print(f"📦 MEXC buffer seeded: {len(candles_mexc)} candles")

        while True:
            try:
                ohlcvs = await ex.watch_ohlcv(SYMBOL, '1m')
                async with lock_mexc:
                    for c in ohlcvs:
                        if candles_mexc and candles_mexc[-1][0] == c[0]:
                            candles_mexc[-1] = c
                        else:
                            candles_mexc.append(c)
            except Exception as e:
                print(f"MEXC kline WS error: {e} — reconnecting...")
                await asyncio.sleep(3)
    finally:
        await ex.close()

# ================== KLINE STREAM — BINANCE ==================
async def kline_bnb():
    ex = ccxtpro.binance({'enableRateLimit': True, 'session': make_session()})
    print("✅ Binance kline WebSocket connecting...")
    try:
        seed = await ex.fetch_ohlcv(SYMBOL, timeframe='1m', limit=CANDLE_BUFFER)
        async with lock_bnb:
            for c in seed:
                candles_bnb.append(c)
        print(f"📦 Binance buffer seeded: {len(candles_bnb)} candles")

        while True:
            try:
                ohlcvs = await ex.watch_ohlcv(SYMBOL, '1m')
                async with lock_bnb:
                    for c in ohlcvs:
                        if candles_bnb and candles_bnb[-1][0] == c[0]:
                            candles_bnb[-1] = c
                        else:
                            candles_bnb.append(c)
            except Exception as e:
                print(f"Binance kline WS error: {e} — reconnecting...")
                await asyncio.sleep(3)
    finally:
        await ex.close()

# ================== BINANCE ORDER FLOW ==================
async def binance_flow():
    global flow
    ex = ccxtpro.binance({'enableRateLimit': True, 'session': make_session()})
    print("✅ Binance order flow WebSocket connecting...")
    try:
        while True:
            try:
                trades = await ex.watch_trades(SYMBOL)
                for t in trades:
                    amt  = float(t['amount'])
                    side = 'sell' if t['info'].get('m', False) else 'buy'
                    if side == 'buy':
                        flow['buy_vol'] += amt
                    else:
                        flow['sell_vol'] += amt

                if time.time() - flow['start_time'] >= FLOW_WINDOW:
                    total = flow['buy_vol'] + flow['sell_vol']
                    ratio = flow['buy_vol'] / total if total > 0 else 0
                    flow['last_ratio']  = ratio
                    flow['last_update'] = time.time()
                    print(
                        f"📊 Flow [{FLOW_WINDOW}s] — "
                        f"BUY {flow['buy_vol']:.2f} ({ratio*100:.1f}%) | "
                        f"SELL {flow['sell_vol']:.2f} ({(1-ratio)*100:.1f}%)"
                    )
                    flow = {
                        'buy_vol':     0.0, 'sell_vol': 0.0,
                        'start_time':  time.time(),
                        'last_ratio':  ratio,
                        'last_update': time.time()
                    }
            except Exception as e:
                print(f"Binance flow WS error: {e}")
                await asyncio.sleep(5)
    finally:
        await ex.close()

# ================== SCANNER ==================
async def scanner():
    global virtual_pos, bb_setup, current_balance, last_miss_logged_idx
    global pending_limit, sl_state, tp_state

    print("✅ Scanner BB20 — FULL MAKER | WEBSOCKET KLINE MODE")
    print(f"   Entry  : Limit buy @ prev_close (maker) — retrace fill")
    print(f"   TP     : Limit sell @ BB20 high (maker) — dinamis bidirectional")
    print(f"   SL     : Plan A → limit maker @ SL price")
    print(f"            Plan B → taker emergency jika harga tembus ${SL_PLAN_B_OFFSET} di bawah SL")
    print(f"   Fee    : Maker {FEE_MAKER*100:.2f}% | Taker {FEE_TAKER*100:.2f}% (Plan B only)")
    print(f"   Data   : WebSocket push (no REST polling)")

    print("⏳ Menunggu buffer candle terisi...")
    while len(candles_mexc) < 25:
        await asyncio.sleep(1)
    print("✅ Buffer siap, scanner aktif")

    while True:
        try:
            async with lock_mexc:
                if len(candles_mexc) < 25:
                    await asyncio.sleep(1)
                    continue
                df_mexc = build_df(candles_mexc)

            prev_mexc   = df_mexc.iloc[-2]
            latest_mexc = df_mexc.iloc[-1]

            bb_low_val  = float(prev_mexc['bb_low'])
            bb_high_val = float(latest_mexc['bb_high'])
            current_idx = int(latest_mexc['ts'])

            body         = prev_mexc['close'] - prev_mexc['open']
            candle_range = prev_mexc['high']  - prev_mexc['low']
            body_ratio   = (body / candle_range) if candle_range > 0 else 0.0
            strong_green = (body > 0 and body_ratio >= BODY_THRESHOLD)
            flow_ok      = flow['last_ratio'] >= FLOW_THRESHOLD

            current_price = float(latest_mexc['close'])
            current_low   = float(latest_mexc['low'])
            current_high  = float(latest_mexc['high'])

            bb_status    = ("CONFIRMED" if bb_setup['confirmed']
                            else ("TOUCHED" if bb_setup['touched'] else "WAITING"))
            limit_status = (
                f"LIMIT({pending_limit['attempts']}/{LIMIT_EXPIRE_CANDLES})"
                if pending_limit['active'] else "NO_LIMIT"
            )
            _rr_prev, _, _ = calc_rr(
                float(prev_mexc['close']),
                float(prev_mexc['close']) * (1 - SL_PCT),
                bb_high_val
            )
            print(
                f"[{bb_status}|{limit_status}] "
                f"{_mark(bb_setup['confirmed'])} BB | "
                f"{_mark(strong_green)} Green {body_ratio:.2f} | "
                f"{_mark(flow_ok)} Flow {flow['last_ratio']*100:.1f}% | "
                f"{_mark(_rr_prev >= MIN_RR)} RR 1:{_rr_prev:.2f} | "
                f"BB: {bb_setup['attempts']}/{WAIT_WINDOW} | "
                f"TP~{bb_high_val:.4f}"
            )

            if (bb_setup['confirmed'] and
                    virtual_pos is None and
                    not pending_limit['active']):
                if not strong_green or not flow_ok:
                    if current_idx != last_miss_logged_idx:
                        last_miss_logged_idx = current_idx
                        log_trade(
                            'MISS', current_price,
                            body_ratio=body_ratio,
                            bb_low=bb_low_val, bb_high=bb_high_val,
                            miss_green=not strong_green,
                            miss_flow=not flow_ok
                        )

            # ── TAHAP 1: BB Touch & Konfirmasi ────────────────────
            if not bb_setup['touched']:
                if prev_mexc['low'] <= bb_low_val:
                    bb_setup['touched'] = True
                    print(f"🔵 BB Touched! Low={prev_mexc['low']:.4f} | BB={bb_low_val:.4f}")
                    if prev_mexc['close'] > bb_low_val:
                        bb_setup['confirmed'] = True
                        bb_setup['confirm_candle_idx'] = current_idx
                        print("✅ BB Confirmed (same candle)")

            elif bb_setup['touched'] and not bb_setup['confirmed']:
                if prev_mexc['close'] > bb_low_val:
                    bb_setup['confirmed'] = True
                    bb_setup['confirm_candle_idx'] = current_idx
                    print("✅ BB Confirmed (next candle)")
                else:
                    print(
                        f"⏳ Waiting close above BB | "
                        f"Close={prev_mexc['close']:.4f} BB={bb_low_val:.4f}"
                    )

            # ── TAHAP 2: Set Limit Entry (Maker) ──────────────────
            if (bb_setup['confirmed'] and
                    virtual_pos is None and
                    not pending_limit['active']):

                if current_idx != bb_setup['confirm_candle_idx']:

                    if current_idx != bb_setup['last_candle_idx']:
                        bb_setup['last_candle_idx'] = current_idx
                        bb_setup['attempts'] += 1
                        print(f"🕐 BB window candle ke-{bb_setup['attempts']}/{WAIT_WINDOW}")

                        if bb_setup['attempts'] > WAIT_WINDOW:
                            print("⏰ BB wait window habis, reset")
                            reset_bb_setup()

                    if (bb_setup['attempts'] <= WAIT_WINDOW and
                            bb_setup['last_candle_idx'] == current_idx):

                        if strong_green and flow_ok:
                            limit_price = float(prev_mexc['close'])

                            rr_gross, rr_net, be_net = calc_rr(
                                limit_price,
                                limit_price * (1 - SL_PCT),
                                bb_high_val,
                                fee_entry=FEE_MAKER,
                                fee_exit=FEE_MAKER
                            )

                            if rr_gross < MIN_RR:
                                print(
                                    f"⛔ RR terlalu rendah: 1:{rr_gross:.2f} "
                                    f"< minimum 1:{MIN_RR:.1f} — skip"
                                )
                                log_trade(
                                    'SKIP_RR', limit_price,
                                    body_ratio=body_ratio,
                                    bb_low=bb_low_val, bb_high=bb_high_val,
                                    rr_gross=rr_gross, rr_net=rr_net,
                                    miss_green=False, miss_flow=False
                                )
                                reset_bb_setup()
                            else:
                                pending_limit['active']            = True
                                pending_limit['price']             = limit_price
                                pending_limit['set_at_idx']        = current_idx
                                pending_limit['last_candle_idx']   = current_idx
                                pending_limit['attempts']          = 0
                                pending_limit['bb_high_at_set']    = bb_high_val
                                pending_limit['body_ratio_at_set'] = body_ratio
                                pending_limit['bb_low_at_set']     = bb_low_val
                                pending_limit['flow_at_set']       = flow['last_ratio']
                                pending_limit['set_candle_ts']     = current_idx

                                await send(
                                    f"📋 <b>LIMIT ENTRY SET (Maker)</b>\n"
                                    f"Limit buy @ {limit_price:.4f} (prev_close)\n"
                                    f"Current   @ {current_price:.4f} "
                                    f"(+{((current_price-limit_price)/limit_price)*100:.2f}% di atas)\n"
                                    f"TP target : {bb_high_val:.4f} (BB20 High)\n"
                                    f"SL target : {limit_price*(1-SL_PCT):.4f} (-{SL_PCT*100:.2f}%)\n"
                                    f"───────────────────────────\n"
                                    f"RR gross : 1:{rr_gross:.2f}\n"
                                    f"RR net   : 1:{rr_net:.2f} | BE: {be_net:.0f}%\n"
                                    f"Fee      : Entry 0% | TP 0% (maker) | "
                                    f"SL Plan A 0% / Plan B {FEE_TAKER*100:.2f}%\n"
                                    f"Menunggu retrace... (max {LIMIT_EXPIRE_CANDLES} candle)\n"
                                    f"Flow: {flow['last_ratio']*100:.1f}% | Body: {body_ratio:.2f}"
                                )
                                reset_bb_setup()

            # ── TAHAP 3: Monitor Limit Entry Fill / Expire ─────────
            if pending_limit['active'] and virtual_pos is None:

                if (current_idx != pending_limit['set_at_idx'] and
                        current_idx != pending_limit['last_candle_idx']):
                    pending_limit['last_candle_idx'] = current_idx
                    pending_limit['attempts'] += 1
                    print(
                        f"⏳ Limit entry candle {pending_limit['attempts']}/{LIMIT_EXPIRE_CANDLES} "
                        f"| Limit @ {pending_limit['price']:.4f} "
                        f"| Low {current_low:.4f}"
                    )

                if pending_limit['attempts'] > LIMIT_EXPIRE_CANDLES:
                    print(f"❌ Limit entry expired ({LIMIT_EXPIRE_CANDLES} candle), skip")
                    log_trade(
                        'LIMIT_EXPIRED', pending_limit['price'],
                        body_ratio=pending_limit['body_ratio_at_set'],
                        bb_low=pending_limit['bb_low_at_set'],
                        bb_high=pending_limit['bb_high_at_set'],
                        limit_price=pending_limit['price'],
                        fill_type='expired'
                    )
                    await send(
                        f"⚠️ <b>LIMIT ENTRY EXPIRED</b>\n"
                        f"Limit @ {pending_limit['price']:.4f}\n"
                        f"Harga tidak retrace dalam {LIMIT_EXPIRE_CANDLES} candle.\n"
                        f"Skip — tunggu setup baru."
                    )
                    reset_pending_limit()

                elif (current_idx != pending_limit['set_at_idx'] and
                        current_low <= pending_limit['price']):
                    flow_at_fill = flow['last_ratio']
                    if flow_at_fill < FLOW_THRESHOLD:
                        set_ts = pending_limit['set_candle_ts']
                        fill_ts = current_idx
                        print(
                            f"⛔ Fill diblokir: flow drop "
                            f"{pending_limit['flow_at_set']*100:.1f}% -> {flow_at_fill*100:.1f}%"
                        )
                        log_trade(
                            'FILL_BLOCKED', pending_limit['price'],
                            body_ratio=pending_limit['body_ratio_at_set'],
                            bb_low=pending_limit['bb_low_at_set'],
                            bb_high=pending_limit['bb_high_at_set'],
                            miss_green=False, miss_flow=True,
                            retrace_candle=pending_limit['attempts'],
                            limit_price=pending_limit['price'],
                            fill_type=(
                                f"flow_drop set:{pending_limit['flow_at_set']*100:.1f}% "
                                f"fill:{flow_at_fill*100:.1f}% | "
                                f"set_ts:{set_ts} fill_ts:{fill_ts}"
                            ),
                            flow_ratio=flow_at_fill
                        )
                        await send(
                            f"⛔ <b>FILL BLOCKED</b>\n"
                            f"Limit @ {pending_limit['price']:.4f} tersentuh, tapi flow drop.\n"
                            f"Flow set : {pending_limit['flow_at_set']*100:.1f}%\n"
                            f"Flow fill: {flow_at_fill*100:.1f}% (< {FLOW_THRESHOLD*100:.0f}%)\n"
                            f"Pending dibatalkan."
                        )
                        reset_pending_limit()
                        continue

                    entry    = pending_limit['price']
                    sl_price = entry * (1 - SL_PCT)
                    tp_price = bb_high_val

                    rr_gross, rr_net, be_net = calc_rr(
                        entry, sl_price, tp_price,
                        fee_entry=FEE_MAKER,
                        fee_exit=FEE_MAKER
                    )

                    virtual_pos = {
                        'entry':          entry,
                        'amount':         current_balance / entry,
                        'time':           datetime.now(),
                        'retrace_candle': pending_limit['attempts'],
                        'flow_at_set':    pending_limit['flow_at_set'],
                        'flow_at_fill':   flow_at_fill,
                        'set_candle_ts':  pending_limit['set_candle_ts'],
                        'fill_candle_ts': current_idx,
                        # [FIX BUG 9] Simpan RR saat entry untuk di-log di baris exit
                        'rr_entry_gross': rr_gross,
                        'rr_entry_net':   rr_net
                    }

                    sl_state['plan']           = 'A'
                    sl_state['sl_price']       = sl_price
                    sl_state['plan_b_trigger'] = sl_price - SL_PLAN_B_OFFSET

                    tp_state['tp_price'] = tp_price
                    tp_state['tp_entry'] = tp_price

                    log_trade(
                        'ENTRY_MAKER', entry,
                        size=virtual_pos['amount'],
                        sl=sl_price, tp_at_entry=tp_price,
                        body_ratio=pending_limit['body_ratio_at_set'],
                        bb_low=pending_limit['bb_low_at_set'],
                        bb_high=tp_price,
                        rr_gross=rr_gross, rr_net=rr_net,
                        retrace_candle=pending_limit['attempts'],
                        limit_price=pending_limit['price'],
                        fill_type=(
                            f"maker | set_ts:{pending_limit['set_candle_ts']} "
                            f"fill_ts:{current_idx} | "
                            f"flow_set:{pending_limit['flow_at_set']*100:.1f}% "
                            f"flow_fill:{flow_at_fill*100:.1f}%"
                        ),
                        flow_ratio=flow_at_fill
                    )
                    await send(
                        f"🚀 <b>ENTRY MAKER FILLED — SOL/USDT</b>\n"
                        f"Entry  @ {entry:.4f} (limit filled)\n"
                        f"Size   : {virtual_pos['amount']:.4f} SOL (~${current_balance:.2f})\n"
                        f"───────────────────────────\n"
                        f"SL Plan A : {sl_price:.4f} (-{SL_PCT*100:.2f}% | limit maker)\n"
                        f"SL Plan B : trigger jika harga < {sl_state['plan_b_trigger']:.4f} "
                        f"(taker emergency)\n"
                        f"TP        : {tp_price:.4f} (BB20 High | limit maker)\n"
                        f"───────────────────────────\n"
                        f"RR gross : 1:{rr_gross:.2f}\n"
                        f"RR net   : 1:{rr_net:.2f} | BE: {be_net:.0f}%\n"
                        f"Fee      : Entry 0% | TP 0% (maker) | SL 0% (Plan A)\n"
                        f"Balance  : ${current_balance:.2f}\n"
                        f"Retrace candle ke-{pending_limit['attempts']}"
                    )
                    reset_pending_limit()
                    # [FIX BUG 4] Skip langsung ke iterasi berikutnya setelah entry fill
                    # agar Tahap 4 tidak langsung dieksekusi di iterasi yang sama
                    await asyncio.sleep(1)
                    continue

            # ── TAHAP 4: Manage Posisi Aktif ──────────────────────
            if virtual_pos is not None:

                # [FIX BUG 9] TP dinamis bidirectional — ikuti BB high naik MAUPUN turun
                # Tidak lagi one-directional. TP selalu mencerminkan BB high terkini.
                new_tp = float(latest_mexc['bb_high'])
                if new_tp != tp_state['tp_price']:
                    old_tp = tp_state['tp_price']
                    tp_state['tp_price'] = new_tp
                    direction = "📈" if new_tp > old_tp else "📉"
                    print(f"{direction} TP limit updated: {old_tp:.4f} → {new_tp:.4f}")

                hit_sl       = False
                hit_tp       = False
                exit_price   = None
                exit_fee     = FEE_MAKER
                sl_plan_used = sl_state['plan']

                if current_high >= tp_state['tp_price']:
                    hit_tp       = True
                    exit_price   = tp_state['tp_price']
                    exit_fee     = FEE_MAKER
                    sl_plan_used = None

                elif current_low <= sl_state['plan_b_trigger']:
                    hit_sl       = True
                    exit_price   = sl_state['plan_b_trigger']
                    exit_fee     = FEE_TAKER
                    sl_plan_used = 'B'
                    print(f"🚨 SL Plan B @ {exit_price:.4f} — emergency taker")

                elif current_price <= sl_state['sl_price']:
                    hit_sl       = True
                    exit_price   = sl_state['sl_price']
                    exit_fee     = FEE_MAKER
                    sl_plan_used = 'A'
                    print(f"📉 SL Plan A @ {exit_price:.4f} — maker limit fill")

                if hit_sl or hit_tp:
                    pnl_gross = (
                        (exit_price - virtual_pos['entry']) * virtual_pos['amount']
                    )
                    fee_total = (
                        virtual_pos['entry'] * FEE_MAKER +
                        exit_price * exit_fee
                    ) * virtual_pos['amount']
                    pnl_net = pnl_gross - fee_total

                    rr_gross, rr_net, _ = calc_rr(
                        virtual_pos['entry'],
                        sl_state['sl_price'],
                        tp_state['tp_price'],
                        fee_entry=FEE_MAKER,
                        fee_exit=exit_fee
                    )

                    label = "✅ TP HIT" if hit_tp else "❌ SL HIT"
                    if hit_tp:
                        exit_type = "wick — limit maker"
                    elif sl_plan_used == 'A':
                        exit_type = "close — limit maker (Plan A)"
                    else:
                        exit_type = "tembus trigger — taker market (Plan B)"

                    current_balance += pnl_net

                    log_trade(
                        'TP' if hit_tp else 'SL',
                        exit_price,
                        pnl_gross=pnl_gross,
                        pnl_net=pnl_net,
                        tp_at_entry=tp_state['tp_entry'],
                        tp_at_exit=tp_state['tp_price'],
                        bb_high=new_tp,
                        rr_gross=rr_gross, rr_net=rr_net,
                        # [FIX BUG 9] Log RR saat entry di baris exit untuk perbandingan
                        rr_entry_gross=virtual_pos.get('rr_entry_gross'),
                        rr_entry_net=virtual_pos.get('rr_entry_net'),
                        retrace_candle=virtual_pos.get('retrace_candle'),
                        sl_plan=sl_plan_used if hit_sl else None,
                        fill_type=(
                            f"{exit_type} | set_ts:{virtual_pos.get('set_candle_ts')} "
                            f"fill_ts:{virtual_pos.get('fill_candle_ts')} exit_ts:{current_idx} | "
                            f"flow_set:{virtual_pos.get('flow_at_set', 0.0)*100:.1f}% "
                            f"flow_fill:{virtual_pos.get('flow_at_fill', 0.0)*100:.1f}% "
                            f"flow_exit:{flow['last_ratio']*100:.1f}%"
                        ),
                        flow_ratio=flow['last_ratio']
                    )
                    await send(
                        f"{label} — SOL/USDT\n"
                        f"Entry  @ {virtual_pos['entry']:.4f} (maker)\n"
                        f"Exit   @ {exit_price:.4f} ({exit_type})\n"
                        f"TP saat exit : {tp_state['tp_price']:.4f}\n"
                        f"PnL gross : ${pnl_gross:.2f}\n"
                        f"PnL net   : ${pnl_net:.2f} "
                        f"(fee exit: {exit_fee*100:.2f}%)\n"
                        f"Balance   : ${current_balance:.2f}"
                    )
                    print(
                        f"{label} | Exit @ {exit_price:.4f} ({exit_type}) | "
                        f"PnL net: ${pnl_net:.2f} | Balance: ${current_balance:.2f}"
                    )
                    virtual_pos = None
                    reset_sl_state()
                    reset_tp_state()
                    # [FIX BUG 2] Reset bb_setup setelah posisi exit agar
                    # setup lama tidak carry-over ke trade berikutnya
                    reset_bb_setup()

        except Exception as e:
            err_msg = f"⚠️ <b>SCANNER ERROR</b>\n{e}"
            print(err_msg)
            try:
                bot.send_message(CHAT_ID, err_msg, parse_mode='HTML')
            except:
                pass
            await asyncio.sleep(2)

        await asyncio.sleep(1)

# ================== MAIN ==================
async def main():
    print("🚀 BOT BB20 — FULL MAKER | WEBSOCKET KLINE MODE STARTED")
    init_journal()
    await asyncio.gather(
        kline_mexc(),
        kline_bnb(),
        binance_flow(),
        scanner(),
        debug_printer()
    )

if __name__ == "__main__":
    while True:
        try:
            asyncio.run(main())
        except Exception as e:
            msg = f"⚠️ <b>BOT CRASHED</b>\nError: {e}\n🔄 Restart in 15s..."
            print(msg)
            try:
                bot.send_message(CHAT_ID, msg, parse_mode='HTML')
            except:
                pass
            time.sleep(15)
        except KeyboardInterrupt:
            bot.send_message(CHAT_ID, "🛑 <b>BOT STOPPED</b>", parse_mode='HTML')
            print("Stopped.")
            break
