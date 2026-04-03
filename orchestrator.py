import time
import requests
import pandas as pd
import pandas_ta as ta
import subprocess
import sys
import os
import json
import yaml
from datetime import datetime

# Настройки рынка
SYMBOL = "BTCUSDT"
TIMEFRAME = "15m"
ADX_PERIOD = 14
CHECK_INTERVAL_SEC = 60 * 15  # Проверка каждые 15 минут

# Файлы
HYBRID_STATE_FILE = "data/state_hybrid.json"
HYBRID_CONFIG_FILE = "configs/config_hybrid.yaml"
BASE_CONFIGS = {
    "sniper": "configs/config_sniper.yaml",
    "trend": "configs/config_trend.yaml",
    "volume": "configs/config_volume.yaml"
}

active_process = None
current_mode = "pause"  # 'sniper', 'trend', 'pause'

def get_time():
    return datetime.now().strftime('%H:%M:%S')

def get_market_data():
    """Получает данные с Binance и рассчитывает ADX"""
    url = f"https://api.binance.com/api/v3/klines?symbol={SYMBOL}&interval={TIMEFRAME}&limit={ADX_PERIOD * 3}"
    try:
        response = requests.get(url, timeout=10)
        response.raise_for_status()
        data = response.json()
        
        df = pd.DataFrame(data, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume', 'close_time', 'qav', 'num_trades', 'taker_base_vol', 'taker_quote_vol', 'ignore'])
        for col in ['high', 'low', 'close']:
            df[col] = df[col].astype(float)
            
        adx_df = ta.adx(df['high'], df['low'], df['close'], length=ADX_PERIOD)
        return adx_df[f'ADX_{ADX_PERIOD}'].iloc[-1]
    except Exception as e:
        print(f"[{get_time()}] ❌ Ошибка получения данных: {e}")
        return None

def has_active_position():
    """Проверяет файл состояния гибрида на наличие открытых позиций"""
    if not os.path.exists(HYBRID_STATE_FILE):
        return False # Стейта еще нет, значит и позиций нет
        
    try:
        with open(HYBRID_STATE_FILE, 'r', encoding='utf-8') as f:
            state = json.load(f)
            # Проверяем, есть ли активная позиция (не null)
            portfolio = state.get("virtual_portfolio", {})
            return portfolio.get("active_position") is not None
    except Exception as e:
        print(f"[{get_time()}] ⚠️ Ошибка чтения стейта: {e}")
        return True # В случае ошибки лучше считать, что позиция есть (безопасность)

def prepare_hybrid_config(base_mode):
    """Создает config_hybrid.yaml на базе нужной стратегии, прописывая единый стейт"""
    base_config_path = BASE_CONFIGS[base_mode]
    try:
        with open(base_config_path, 'r', encoding='utf-8') as f:
            config = yaml.safe_load(f)
            
        # Форсируем использование гибридного стейт-файла
        # (Предполагается, что в твоем yaml есть секция state или data, 
        # если путь хранится иначе - поправь этот блок)
        if 'storage' not in config:
            config['storage'] = {}
        config['storage']['state_file'] = HYBRID_STATE_FILE
        
        with open(HYBRID_CONFIG_FILE, 'w', encoding='utf-8') as f:
            yaml.dump(config, f, default_flow_style=False)
            
    except Exception as e:
        print(f"[{get_time()}] ❌ Ошибка создания гибридного конфига: {e}")

def switch_mode(target_mode):
    """Переключает текущего бота на новый режим"""
    global active_process, current_mode
    
    # Если режим не меняется, ничего не делаем
    if current_mode == target_mode:
        return

    # Шаг 1: Проверяем, можно ли переключаться
    if has_active_position():
        print(f"[{get_time()}] ⏳ Тренд изменился на {target_mode.upper()}, но есть АКТИВНАЯ ПОЗИЦИЯ. Ждем закрытия сделки режимом {current_mode.upper()}...")
        return
        
    # Шаг 2: Убиваем текущий процесс (сделок нет, это безопасно)
    if active_process is not None:
        print(f"[{get_time()}] 🛑 Останавливаем логику: {current_mode.upper()}")
        active_process.terminate()
        active_process.wait()
        active_process = None

    current_mode = target_mode
    
    # Шаг 3: Запускаем новый режим
    if target_mode == "pause":
        print(f"[{get_time()}] ⏸ Бот переведен в режим ПАУЗЫ (рынок неопределен).")
    else:
        prepare_hybrid_config(target_mode)
        print(f"[{get_time()}] 🚀 Запускаем логику: {target_mode.upper()} (Стейт: Гибрид)")
        
        cmd = [sys.executable, "src/main.py", "--config", HYBRID_CONFIG_FILE]
        active_process = subprocess.Popen(cmd)

def orchestrate():
    adx = get_market_data()
    if adx is None:
        return
        
    print(f"\n[{get_time()}] 📊 Текущий ADX: {adx:.2f}")
    
    # Определяем желаемый режим гибрида
    target_mode = "pause"
    if adx > 40:
        # Для сильного тренда выбираем конфиг Trend (можешь поменять на Volume)
        target_mode = "trend" 
    elif adx < 25:
        # Для флэта выбираем Sniper
        target_mode = "sniper"
    else:
        # 25-40: Зона смерти
        target_mode = "pause"
        
    switch_mode(target_mode)

if __name__ == "__main__":
    print(f"[{get_time()}] 🧠 Гибридный Мозг (Дирижер) запущен!")
    print(f"[{get_time()}] Баланс и позиции синхронизируются через {HYBRID_STATE_FILE}")
    
    # Если стейта нет, можем создать его с балансом 1000$ (опционально)
    if not os.path.exists(HYBRID_STATE_FILE):
        os.makedirs("data", exist_ok=True)
        init_state = {
            "virtual_portfolio": {
                "balance_usd": 1000.0,
                "active_position": None,
                "daily_pnl": 0.0
            },
            "trade_history": []
        }
        with open(HYBRID_STATE_FILE, 'w') as f:
            json.dump(init_state, f)
        print(f"[{get_time()}] 💰 Создан новый портфель на $1000")

    try:
        orchestrate()
        while True:
            time.sleep(CHECK_INTERVAL_SEC)
            orchestrate()
    except KeyboardInterrupt:
        if active_process:
            active_process.terminate()
        print(f"\n[{get_time()}] Дирижер остановлен.")
        sys.exit(0)