"""
Alpha158 精选因子实现（轻量版）
从Qlib Alpha158的158个因子中精选15个，用于A股选股评分。
仅依赖numpy，数据源为腾讯API K线（open/close/high/low/volume）。

因子分类：
  Trend:   MA5/MA20, MA20/MA60, ROC10, VOL10/MA10
  MR:      RSI14, CCI, BIAS20
  Volatility: ATR14_NORM, STDDEV10  
  Volume:  VRATIO, CMF
  Composite: MACD, KDJ_K

每个因子输出0~10分，总分0~150 -> 归一化到0~100。
"""
import numpy as np

def compute(c, v, h=None, l=None):
    """
    核心计算函数
    c: 收盘价数组 (numpy array, 至少60个元素)
    v: 成交量数组
    h: 最高价数组 (可选，默认用c计算)
    l: 最低价数组 (可选，默认用c计算)
    返回: {'total': 总分, 'factors': {因子名: 得分}}
    """
    if h is None: h = c
    if l is None: l = c
    if len(c) < 60: return {'total': 0, 'factors': {'ERROR': 0}}
    
    n = len(c)
    scores = {}
    
    # ── Trend 趋势因子 ──
    # T1: MA5/MA20 (短线强度)
    ma5 = np.mean(c[-5:])
    ma20 = np.mean(c[-20:])
    ma5_ratio = (ma5 / ma20 - 1) * 100
    scores['MA5_MA20'] = min(10, max(0, (ma5_ratio + 5) * 1.5))  # -5%~+5% → 0~15分，截断到10
    
    # T2: MA20/MA60 (趋势方向)
    ma60 = np.mean(c[-60:])
    ma20_ratio = (ma20 / ma60 - 1) * 100
    scores['MA20_MA60'] = min(10, max(0, ma20_ratio * 2))  # 0%~5% → 0~10分
    
    # T3: ROC10 (10日动量)
    roc10 = (c[-1] / c[-11] - 1) * 100 if n >= 11 else 0
    scores['ROC10'] = min(10, max(0, (roc10 + 5) * 1.0))  # -5%~+5% → 0~10分
    
    # T4: VOL10/MA10 (量能趋势)
    vol10 = np.mean(v[-10:])
    vol_ma10 = v[-1] / max(vol10, 0.01)
    scores['VOL_TREND'] = min(10, max(0, 10 - abs(vol_ma10 - 1) * 5))  # 1附近最高
    
    # ── Mean Reversion 均值回归因子 ──
    # M1: RSI14 (已有)
    rsi_val = _rsi(c, 14)[-1]
    scores['RSI14'] = min(10, max(0, 10 - abs(rsi_val - 45) * 0.3))  # 45附近最高(回调区间)
    
    # M2: CCI (商品通道指数)
    cci_val = _cci(c, h, l, 20)
    scores['CCI'] = min(10, max(0, 10 - abs(cci_val) * 0.05))  # 0附近最高
    
    # M3: BIAS20 (20日乖离率)
    bias = (c[-1] / ma20 - 1) * 100
    scores['BIAS20'] = min(10, max(0, 10 - abs(bias) * 3))  # 0附近最高
    
    # ── Volatility 波动率因子 ──
    # V1: ATR14 (归一化)
    atr = _atr(c, h, l, 14)
    atr_norm = atr / ma20 * 100
    scores['ATR'] = min(10, max(0, 10 - abs(atr_norm - 3) * 1.5))  # 3%附近最好
    
    # V2: STDDEV10
    rets = np.diff(c[-11:]) / c[-11:-1]
    std10 = np.std(rets) * 100
    scores['STDDEV'] = min(10, max(0, 10 - abs(std10 - 2) * 2))  # 2%附近最好
    
    # ── Volume 量价因子 ──
    # U1: VRATIO (量比)
    vr = v[-1] / max(np.mean(v[-6:-1]), 1)
    scores['VRATIO'] = min(10, max(0, 10 - vr * 8))  # 越小越好（缩量）
    
    # U2: CMF (Chaikin资金流)
    cmf = _cmf(c, h, l, v, 20)
    scores['CMF'] = min(10, max(0, (cmf + 0.5) * 10))  # -0.5~+0.5 → 0~10分
    
    # ── Composite 复合指标 ──
    # C1: MACD柱（日线级别）
    macd_hist = _macd(c)
    scores['MACD'] = min(10, max(0, macd_hist * 50 + 5))  # -0.1~+0.1 → 0~10分
    
    # C2: KDJ_K (随机指标)
    kdj_k = _kdj(c, h, l)[0]
    scores['KDJ_K'] = min(10, max(0, 10 - abs(kdj_k - 30) * 0.15))  # 30附近最好(回调)
    
    # 总分 (加权)
    weights = {
        'MA5_MA20': 1.0, 'MA20_MA60': 1.5, 'ROC10': 0.8, 'VOL_TREND': 0.5,
        'RSI14': 1.2, 'CCI': 0.8, 'BIAS20': 1.0,
        'ATR': 0.5, 'STDDEV': 0.5,
        'VRATIO': 1.5, 'CMF': 0.8,
        'MACD': 0.8, 'KDJ_K': 0.8,
    }
    
    weighted = sum(scores[k] * weights.get(k, 1) for k in scores)
    max_weighted = sum(10 * weights.get(k, 1) for k in scores)
    total = min(100, weighted / max_weighted * 100)
    
    return {'total': round(float(total), 1), 'factors': {k: round(float(v), 1) for k, v in scores.items()}}

# ── 工具函数 ──

def _rsi(c, p=14):
    d = np.diff(c)
    g = np.where(d > 0, d, 0.0)
    ll = np.where(d < 0, -d, 0.0)
    ag = np.convolve(g, np.ones(p)/p, 'valid')
    al = np.convolve(ll, np.ones(p)/p, 'valid')
    if len(ag) == 0: return [50.0]
    rs = np.divide(ag, al, out=np.ones_like(ag)*100, where=al>0)
    return 100 - 100 / (1 + rs)

def _cci(c, h, l, p=20):
    """商品通道指数"""
    tp = (c + h + l) / 3
    if len(tp) < p: return 0
    tp_mean = np.mean(tp[-p:])
    tp_mad = np.mean(np.abs(tp[-p:] - tp_mean))
    if tp_mad == 0: return 0
    return (tp[-1] - tp_mean) / (0.015 * tp_mad)

def _atr(c, h, l, p=14):
    """平均真实波幅"""
    if len(c) < p + 1: return 0
    tr = np.maximum(h[-p:] - l[-p:], 
                    np.maximum(np.abs(h[-p:] - c[-p-1:-1]), 
                               np.abs(l[-p:] - c[-p-1:-1])))
    return np.mean(tr)

def _cmf(c, h, l, v, p=20):
    """Chaikin资金流"""
    if len(c) < p: return 0
    mfv = ((c[-p:] - l[-p:]) - (h[-p:] - c[-p:])) / (h[-p:] - l[-p:] + 1e-10) * v[-p:]
    return np.sum(mfv) / max(np.sum(v[-p:]), 1)

def _macd(c):
    """MACD柱状图值"""
    if len(c) < 26: return 0
    ema12 = _ema(c, 12)
    ema26 = _ema(c, 26)
    dif = ema12[-1] - ema26[-1]
    # 简化的dea（用9日ema代替）
    dea = _ema(np.array([ema12[i] - ema26[i] for i in range(len(ema12))]), 9)[-1]
    return dif - dea

def _kdj(c, h, l, p=9):
    """KDJ随机指标K值"""
    if len(c) < p: return [50, 50]
    hh = np.max(h[-p:])
    ll = np.min(l[-p:])
    if hh == ll: return [50, 50]
    rsv = (c[-1] - ll) / (hh - ll) * 100
    return [rsv, rsv]

def _ema(arr, p):
    """指数移动平均"""
    result = np.zeros(len(arr))
    if len(arr) == 0: return result
    k = 2 / (p + 1)
    result[0] = arr[0]
    for i in range(1, len(arr)):
        result[i] = arr[i] * k + result[i-1] * (1 - k)
    return result
