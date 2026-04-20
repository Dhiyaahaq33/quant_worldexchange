import ccxt
import time
import telebot
import pandas as pd
import threading
import urllib3
import os
from flask import Flask, render_template
from datetime import datetime
import requests

app = Flask(__name__)

# --- CONFIGURATION ---
TOKEN = "8361912847:AAHp6txd_IL__TaYL0m21y3MOLM_0MdzudE" # 
CHAT_ID = "6052270268" # 
bot = telebot.TeleBot(TOKEN)

# Ganti ke Binance
exchange = ccxt.binance({'enableRateLimit': True})
ALL_USDT_SYMBOLS = []
active_alerts = {}
last_alerts = {}

@app.route('/')
def home():
    return render_template('index.html')

def fetch_all_markets():
    global ALL_USDT_SYMBOLS
    try:
        markets = exchange.load_markets()
        # Filter hanya pair USDT yang aktif
        ALL_USDT_SYMBOLS = [s for s in markets if s.endswith('/USDT') and markets[s]['active']]
        print(f"✅ Binance Intelligence Ready: {len(ALL_USDT_SYMBOLS)} Assets Scanned.")
    except Exception as e:
        print(f"❌ Error loading markets: {e}")

def get_market_analysis(symbol):
    try:
        ohlcv = exchange.fetch_ohlcv(symbol, '1h', limit=100)
        if not ohlcv or len(ohlcv) < 20: return None        
        df = pd.DataFrame(ohlcv, columns=['ts', 'open', 'high', 'low', 'close', 'vol'])
        
        # RSI Calculation
        delta = df['close'].diff()
        gain = (delta.where(delta > 0, 0)).rolling(window=14).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(window=14).mean()
        df['rsi'] = 100 - (100 / (1 + (gain / loss)))
        
        # MPI & Vol Spike
        green_vol = df[df['close'] > df['open']]['vol'].sum()
        red_vol = df[df['close'] < df['open']]['vol'].sum()
        mpi = (green_vol / (green_vol + red_vol)) * 100 if (green_vol + red_vol) > 0 else 50
        
        last = df.iloc[-1]
        df['vol_avg'] = df['vol'].rolling(window=20).mean()
        vol_spike_ratio = last['vol'] / df['vol_avg'].iloc[-1] if df['vol_avg'].iloc[-1] > 0 else 0
        
        curr_p = last['close']
        signal = "⚖️ NEUTRAL"
        if last['rsi'] < 30: signal = "🚀 STRONG ACCUMULATION"
        elif last['rsi'] > 70: signal = "🔴 DISTRIBUTION / SELL"

        # Trajectory Targets
        whale_strength = mpi / 100
        vol_factor = max(vol_spike_ratio, 1.0)
        
        if "ACCUMULATION" in signal:
            tp1 = curr_p * 1.02
            tp2 = curr_p * (1 + (0.05 * vol_factor))
            tp3 = curr_p * (1 + (0.10 * vol_factor))
        else:
            tp1 = tp2 = tp3 = curr_p

        grade = "C"
        if (last['rsi'] < 30 or last['rsi'] > 70) and vol_spike_ratio > 2.0: grade = "A+ (PERFECT)"

        return {
            'price': curr_p,
            'tp1': tp1, 'tp2': tp2, 'tp3': tp3,
            'rsi': last['rsi'], 'mpi': mpi,
            'signal': signal, 'vol_spike': vol_spike_ratio,
            'grade': grade
        }
    except: return None

def whale_detector():
    while True:
        # Scan top volume coins only to save rate limit (optional)
        for symbol in ALL_USDT_SYMBOLS[:100]: 
            data = get_market_analysis(symbol)
            if not data: continue
            
            coin = symbol.split('/')[0]
            active_alerts[coin] = {**data, 'time': datetime.now().strftime('%H:%M:%S')}
            
            if data['grade'] == "A+ (PERFECT)" and last_alerts.get(coin) != data['signal']:
                msg = f"🌟 **BINANCE WHALE ALERT**\n🪙 {coin}\n📢 {data['signal']}\n💵 Price: ${data['price']:.4f}\n🎯 TP1: ${data['tp1']:.4f}"
                bot.send_message(CHAT_ID, msg, parse_mode='Markdown')
                last_alerts[coin] = data['signal']
            time.sleep(0.5)
        time.sleep(30)

@app.route('/api/intelligence')
def get_intelligence():
    reports = []
    for coin, info in active_alerts.items():
        reports.append({
            "asset": coin, "signal": info['signal'], "grade": info['grade'],
            "time": info['time'], "price": f"{info['price']:.6f}",
            "tp1": f"{info['tp1']:.6f}", "tp2": f"{info['tp2']:.6f}", "tp3": f"{info['tp3']:.6f}",
            "rsi": f"{info['rsi']:.2f}", "mpi": f"{info['mpi']:.1f}", "vol": f"{info['vol_spike']:.1f}"
        })
    return {"reports": reports}

if __name__ == "__main__":
    fetch_all_markets()
    threading.Thread(target=whale_detector, daemon=True).start()
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port)
