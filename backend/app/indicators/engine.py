import pandas as pd
import ta
import numpy as np
from app.models.strategy import IndicatorDef

def apply_indicators(df: pd.DataFrame, indicators: list[IndicatorDef]) -> pd.DataFrame:
    # We will compute columns for each requested indicator.
    # To avoid recomputing, we can use a set of signatures.
    
    # Let's add basic price references if they are treated as indicators
    df['close'] = df['close'].astype(float)
    df['open'] = df['open'].astype(float)
    df['high'] = df['high'].astype(float)
    df['low'] = df['low'].astype(float)
    df['volume'] = df['volume'].astype(float)
    
    computed = set()
    
    for ind in indicators:
        name = ind.name.lower()
        params = ind.params
        
        # Create a unique key for the column
        param_str = "_".join([f"{k}{v}" for k, v in sorted(params.items())])
        col_name = f"{name}_{param_str}" if param_str else name
        
        if col_name in computed or col_name in df.columns:
            continue
            
        if name == 'ema':
            period = int(params.get('period', 20))
            df[col_name] = ta.trend.EMAIndicator(df['close'], window=period).ema_indicator()
        elif name == 'sma':
            period = int(params.get('period', 20))
            df[col_name] = ta.trend.SMAIndicator(df['close'], window=period).sma_indicator()
        elif name == 'rsi':
            period = int(params.get('period', 14))
            df[col_name] = ta.momentum.RSIIndicator(df['close'], window=period).rsi()
        elif name == 'macd':
            fast = int(params.get('fast', 12))
            slow = int(params.get('slow', 26))
            sign = int(params.get('signal', 9))
            macd_obj = ta.trend.MACD(df['close'], window_slow=slow, window_fast=fast, window_sign=sign)
            df[col_name] = macd_obj.macd()
            df[f"macd_signal_{param_str}"] = macd_obj.macd_signal()
            df[f"macd_diff_{param_str}"] = macd_obj.macd_diff()
            computed.add(f"macd_signal_{param_str}")
            computed.add(f"macd_diff_{param_str}")
        elif name == 'adx':
            period = int(params.get('period', 14))
            adx_obj = ta.trend.ADXIndicator(df['high'], df['low'], df['close'], window=period)
            df[col_name] = adx_obj.adx()
        elif name == 'bollinger_upper' or name == 'bollinger_lower' or name == 'bollinger_mid' or name == 'bbands':
            period = int(params.get('period', 20))
            std = float(params.get('std_dev', params.get('std', 2.0)))
            bb_obj = ta.volatility.BollingerBands(df['close'], window=period, window_dev=std)
            # Add all bands at once
            param_str_bb = f"period{period}_std_dev{std}"
            df[f"bb_upper_{param_str_bb}"] = bb_obj.bollinger_hband()
            df[f"bb_lower_{param_str_bb}"] = bb_obj.bollinger_lband()
            df[f"bb_mid_{param_str_bb}"] = bb_obj.bollinger_mavg()
            computed.update([f"bb_upper_{param_str_bb}", f"bb_lower_{param_str_bb}", f"bb_mid_{param_str_bb}"])
            
        elif name == 'stoch':
            k_period = int(params.get('k_period', 14))
            d_period = int(params.get('d_period', 3))
            stoch_obj = ta.momentum.StochasticOscillator(df['high'], df['low'], df['close'], window=k_period, smooth_window=d_period)
            df[f"stoch_k_k_period{k_period}_d_period{d_period}"] = stoch_obj.stoch()
            df[f"stoch_d_k_period{k_period}_d_period{d_period}"] = stoch_obj.stoch_signal()
            computed.update([f"stoch_k_k_period{k_period}_d_period{d_period}", f"stoch_d_k_period{k_period}_d_period{d_period}"])
            
        elif name == 'volume_sma':
            period = int(params.get('period', 20))
            df[col_name] = ta.trend.SMAIndicator(df['volume'], window=period).sma_indicator()
            
        computed.add(col_name)
        
    return df

def get_indicator_col_name(ind: IndicatorDef) -> str:
    name = ind.name.lower()
    if name in ['close', 'open', 'high', 'low', 'volume']:
        return name
    params = ind.params
    param_str = "_".join([f"{k}{v}" for k, v in sorted(params.items())])
    return f"{name}_{param_str}" if param_str else name
