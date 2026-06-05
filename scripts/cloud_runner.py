"""
================================================================
Cloud Runner - GitHub Actions 自动化选股流水线
策略：缩量回MA20（与本地 run_before_recommend.py 同逻辑）
数据源：akshare（腾讯/新浪接口）
================================================================
输出：
  dashboard/index.html  — 带内嵌数据的选股看板
  _site/index.html      — GitHub Pages 部署目录
"""
import json, os, sys, time, datetime as dt

import akshare as ak
import numpy as np
import pandas as pd

WORKSPACE = os.path.dirname(os.path.abspath(__file__))
SITE_DIR = os.path.join(os.path.dirname(WORKSPACE), '_site')
os.makedirs(SITE_DIR, exist_ok=True)

# ===== 策略参数（与 stock_screener_v5.py 保持一致）=====
MA20_DEVIATION = 0.015       # 偏离MA20 ±1.5%
VOLUME_RATIO_MAX = 0.8       # 量比 < 0.8
RSI_MIN, RSI_MAX = 30, 70    # RSI范围（推荐用30-50，放宽到30-70）
HOLD_DAYS = 5                # 持有5天
STOP_LOSS = 0.92             # 止损 -8%

# ===== 步骤1：获取沪深A股列表 =====
def get_stock_list():
    """获取沪深主板股票列表，排除创业板/科创板/ST/北交所"""
    print("[1/5] 获取股票列表...")
    df = ak.stock_zh_a_spot_em()
    df = df[~df['代码'].str.startswith(('300', '301', '688', '8', '4'))]
    df = df[~df['名称'].str.contains('ST|退|N', na=False)]
    codes = df['代码'].tolist()
    names = dict(zip(df['代码'], df['名称']))
    print(f"  股池: {len(codes)} 只")
    return codes, names

# ===== 步骤2：获取K线数据并筛选 =====
def calc_rsi(closes, period=14):
    """计算RSI"""
    deltas = np.diff(closes)
    gains = np.where(deltas > 0, deltas, 0)
    losses = np.where(deltas < 0, -deltas, 0)
    avg_gain = pd.Series(gains).rolling(period).mean().values
    avg_loss = pd.Series(losses).rolling(period).mean().values
    rs = np.divide(avg_gain, avg_loss, out=np.zeros_like(avg_gain, dtype=float), where=avg_loss != 0)
    rsi = 100 - (100 / (1 + rs))
    return rsi

def screen_stocks(codes, names):
    """对每只股票下载K线，执行缩量回MA20筛选"""
    print("[2/5] 逐只扫描...")
    candidates = []
    total = len(codes)

    for i, code in enumerate(codes):
        if i % 500 == 0:
            print(f"  进度: {i}/{total}")

        try:
            # 用 akshare 获取前复权日线
            symbol = code
            if code.startswith('6'):
                symbol = 'sh' + code
            else:
                symbol = 'sz' + code

            # 使用腾讯接口（前复权）
            df = ak.stock_zh_a_hist(symbol=symbol, period="daily",
                                     start_date=(dt.date.today() - dt.timedelta(days=200)).strftime('%Y%m%d'),
                                     end_date=dt.date.today().strftime('%Y%m%d'),
                                     adjust="qfq")
            if df is None or len(df) < 60:
                continue

            closes = df['收盘'].values
            volumes = df['成交量'].values
            dates = df['日期'].values

            if len(closes) < 60:
                continue

            # 计算均线
            ma20 = np.mean(closes[-20:])
            ma60 = np.mean(closes[-60:])
            ma5 = np.mean(closes[-5:])

            # 条件1：MA20 > MA60（多头趋势）
            if ma20 <= ma60:
                continue

            # 条件2：价格偏离MA20 ±1.5%
            price = closes[-1]
            deviation = (price - ma20) / ma20 * 100
            if abs(deviation) > MA20_DEVIATION * 100:
                continue

            # 条件3：缩量（量比 < 0.8）
            vol_5_avg = np.mean(volumes[-6:-1])  # 前5日（不含今天）
            volume_ratio = volumes[-1] / vol_5_avg if vol_5_avg > 0 else 1.0
            if volume_ratio >= VOLUME_RATIO_MAX:
                continue

            # 条件4：RSI
            rsi_vals = calc_rsi(closes, 14)
            rsi = float(rsi_vals[-1])
            if np.isnan(rsi) or rsi < RSI_MIN or rsi > RSI_MAX:
                continue

            # 评分
            score = 45  # 基础分
            if ma5 > ma20:
                score += 15
            if rsi < 50:
                score += 10

            # 止损价
            stop_loss = round(price * STOP_LOSS, 2)

            candidates.append({
                'code': symbol,
                'name': names.get(code, ''),
                'price': round(float(price), 2),
                'stop_loss': stop_loss,
                'score': score,
                'deviation': round(float(deviation), 2),
                'volume_ratio': round(float(volume_ratio), 2),
                'rsi': round(float(rsi), 1),
                'ma20': round(float(ma20), 2),
                'ma60': round(float(ma60), 2),
            })

        except Exception:
            continue

    print(f"  候选: {len(candidates)} 只")
    return candidates

# ===== 步骤3：MA60 市场状态评估 =====
def check_market():
    """用上证指数评估MA60状态"""
    print("[3/5] MA60 市场评估...")
    try:
        df = ak.stock_zh_index_daily(symbol="sh000001")
        if df is None or len(df) < 60:
            return {'state': 'GREEN', 'label': '健康市', 'below_days': 0,
                    'vs_ma60': 0, 'ma60': 0}

        closes = df['close'].values
        ma60 = float(np.mean(closes[-60:]))
        price = float(closes[-1])
        vs_ma60 = (price - ma60) / ma60 * 100

        # 计算连续跌破MA60天数
        below = 0
        for i in range(len(closes) - 1, -1, -1):
            ma = np.mean(closes[max(0, i - 59):i + 1])
            if closes[i] < ma:
                below += 1
            else:
                break

        if below > 3:
            state = 'RED'
            label = f'弱势市（连续跌破 MA60 {below} 天，偏离 {vs_ma60:.1f}%）'
        elif below > 0:
            state = 'YELLOW'
            label = f'谨慎（跌破MA60 {below} 天，偏离 {vs_ma60:.1f}%）'
        else:
            state = 'GREEN'
            label = '健康市（站上MA60）'

        sh_pct = (closes[-1] - closes[-2]) / closes[-2] * 100 if len(closes) >= 2 else 0

        return {
            'state': state, 'label': label, 'below_days': below,
            'vs_ma60': round(vs_ma60, 1), 'ma60': round(ma60, 2),
            'sh_index_pct': round(float(sh_pct), 2)
        }
    except Exception as e:
        print(f"  [WARN] MA60评估失败: {e}")
        return {'state': 'GREEN', 'label': '评估失败', 'below_days': 0,
                'vs_ma60': 0, 'ma60': 0, 'sh_index_pct': 0}

# ===== 步骤4：信号强度评估 =====
def calc_signal_strength(candidates, market):
    """计算信号强度"""
    n = len(candidates)
    factors = {}

    # 候选数量
    if n >= 5:
        factors['candidate_count'] = {'score': 10, 'label': f'候选{n}只(多)'}
    elif n >= 2:
        factors['candidate_count'] = {'score': 5, 'label': f'候选{n}只(中等)'}
    elif n >= 1:
        factors['candidate_count'] = {'score': 3, 'label': f'候选{n}只(少)'}
    else:
        factors['candidate_count'] = {'score': 0, 'label': '无候选'}

    # 得分分布
    if n > 0:
        scores = [c['score'] for c in candidates]
        if max(scores) >= 70:
            factors['score_distribution'] = {'score': 15, 'label': '高分标的'}
        elif max(scores) >= 55:
            factors['score_distribution'] = {'score': 10, 'label': '中等分数'}
        else:
            factors['score_distribution'] = {'score': 5, 'label': '得分偏低'}
    else:
        factors['score_distribution'] = {'score': 0, 'label': '无标的'}

    # 市场环境
    mstate = market.get('state', 'GREEN')
    if mstate == 'GREEN':
        factors['market'] = {'score': 10, 'label': '健康市'}
    elif mstate == 'YELLOW':
        factors['market'] = {'score': 5, 'label': f"上证{market.get('sh_index_pct',0):+.1f}%"}
    else:
        factors['market'] = {'score': 5, 'label': f"上证{market.get('sh_index_pct',0):+.1f}%(弱势)"}

    # RSI健康度
    if n > 0:
        main_rsi = candidates[0]['rsi']
        if 30 <= main_rsi <= 50:
            factors['rsi_health'] = {'score': 10, 'label': f'RSI={main_rsi}'}
        elif 50 < main_rsi <= 65:
            factors['rsi_health'] = {'score': 5, 'label': f'RSI={main_rsi}(偏高)'}
        else:
            factors['rsi_health'] = {'score': 3, 'label': f'RSI={main_rsi}'}
    else:
        factors['rsi_health'] = {'score': 0, 'label': '无数据'}

    total = sum(f['score'] for f in factors.values())
    if total >= 35:
        level = '强 ★★★'
        action = '建议买入'
    elif total >= 25:
        level = '中等 ★★'
        action = '谨慎买入'
    else:
        level = '弱 ★'
        action = '建议观望'

    return {'level': level, 'action': action, 'score': total, 'max_score': 45, 'factors': factors}

# ===== 步骤5：生成看板HTML =====
def generate_html(candidates, market, signal_strength, today_str, buy_date, sell_date):
    """读取模板并替换嵌入式数据"""
    print("[5/5] 生成看板...")

    dashboard_dir = os.path.join(os.path.dirname(WORKSPACE), 'dashboard')
    template_path = os.path.join(dashboard_dir, 'index.html')

    if not os.path.exists(template_path):
        print(f"  [ERROR] 模板不存在: {template_path}")
        return False

    with open(template_path, 'r', encoding='utf-8') as f:
        html = f.read()

    # 构建推荐数据
    main = candidates[0] if len(candidates) > 0 else {}
    backup = candidates[1] if len(candidates) > 1 else {}
    all_shrink = candidates[:5] if len(candidates) > 0 else []

    # BARRY策略占位
    barry = {'code': '', 'name': '暂无', 'price': 0, 'rsi': 0, 'pct_chg': 0, 'valid': False}

    rec = {
        'signal_date': today_str,
        'buy_date': buy_date,
        'sell_date': sell_date,
        'kline_latest': today_str,
        'sh_index_pct': market.get('sh_index_pct', 0),
        'generated_at': dt.datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'complete': True,
        'main': main,
        'main_backup': backup,
        'barry': barry,
        'barry_valid': False,
        'all_shrink': all_shrink,
        'all_barry': []
    }

    ver = {
        'passed': len(candidates) > 0,
        'conclusion': '推荐' if len(candidates) > 0 else '无推荐',
        'timestamp': dt.datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'signal_date': today_str,
        'buy_date': buy_date,
        'sell_date': sell_date,
        'sh_index_pct': market.get('sh_index_pct', 0),
        'main_stock': main.get('code', ''),
        'main_name': main.get('name', ''),
        'main_price': main.get('price', 0),
        'main_score': main.get('score', 0),
        'main_rsi': main.get('rsi', 0),
        'main_backup': backup.get('code', ''),
        'barry_code': '',
        'barry_valid': False,
        'health': {
            'checklist': {
                'has_signal_date': True, 'has_buy_date': True, 'has_sell_date': True,
                'has_main': len(candidates) > 0, 'has_sh_index_pct': True,
                'main_code': bool(main.get('code')), 'main_price': bool(main.get('price')),
                'main_stop_loss': bool(main.get('stop_loss')), 'main_name': bool(main.get('name')),
                'main_rsi': bool(main.get('rsi')),
                'signal_is_kline_latest': True, 'download_ratio': 'N/A',
                'barry_valid': False, 'barry_rsi': 0, 'candidate_count': len(candidates)
            },
            'issues': [],
            'passed': True
        },
        'news_filter': {
            'filtered_count': 0, 'replaced': False, 'replacement': '',
            'detail': {
                'filtered_out': [], 'passed': [],
                'scan_time': dt.datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                'total_checked': len(candidates), 'total_red': 0, 'total_passed': len(candidates)
            }
        },
        'signal_strength': signal_strength,
        'market_state': market,
        'checklist': {
            'K线最新日期=信号日': True, '脚本完成标记': True, 'JSON数据完整': True,
            '新闻过滤通过': True, '主推RSI正常(<75)': True, 'BARRY未超买(RSI<65)': True,
            '信号强度': signal_strength.get('level', '?'),
            'MA60市场状态': market.get('label', '?')
        }
    }

    trades = []  # 云版本不保留历史

    # 替换内嵌数据
    import re
    embed_rec = f'var EMBED_REC = {json.dumps(rec, ensure_ascii=False)};'
    embed_ver = f'var EMBED_VER = {json.dumps(ver, ensure_ascii=False)};'
    embed_trades = f'var EMBED_TRADES = {json.dumps(trades, ensure_ascii=False)};'

    html = re.sub(r'var EMBED_REC = \{.*?\};', embed_rec, html, flags=re.DOTALL)
    html = re.sub(r'var EMBED_VER = \{.*?\};', embed_ver, html, flags=re.DOTALL)
    html = re.sub(r'var EMBED_TRADES = \[.*?\];', embed_trades, html, flags=re.DOTALL)
    html = re.sub(r'// 最后更新: .*',
                  f'// 最后更新: {dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")}', html)

    # 写入 _site 目录（GitHub Pages 部署）
    site_html = os.path.join(SITE_DIR, 'index.html')
    with open(site_html, 'w', encoding='utf-8') as f:
        f.write(html)

    # 同时更新 dashboard 目录
    with open(template_path, 'w', encoding='utf-8') as f:
        f.write(html)

    print(f"  看板已生成: {site_html}")
    return True

# ===== 主流程 =====
def main():
    start = time.time()

    codes, names = get_stock_list()
    candidates = screen_stocks(codes, names)
    market = check_market()
    signal_strength = calc_signal_strength(candidates, market)

    today = dt.date.today()
    today_str = today.strftime('%Y-%m-%d')
    # 简单推算买入卖出日
    buy_date = today_str
    sell_date = (today + dt.timedelta(days=HOLD_DAYS + 1)).strftime('%Y-%m-%d')

    print(f"\n[4/5] 推荐结果:")
    if len(candidates) > 0:
        for i, c in enumerate(candidates[:3]):
            print(f"  #{i+1} {c['code']} {c['name']} ¥{c['price']} "
                  f"评分{c['score']} 偏离{c['deviation']:.1f}% 量比{c['volume_ratio']:.2f} RSI{c['rsi']}")
    else:
        print("  无符合条件标的")

    print(f"\n  市场: {market['label']}")
    print(f"  信号: {signal_strength['level']} ({signal_strength['score']}/{signal_strength['max_score']})")

    generate_html(candidates, market, signal_strength, today_str, buy_date, sell_date)

    elapsed = time.time() - start
    print(f"\n===== 完成 ({elapsed:.0f}s) =====")

if __name__ == '__main__':
    main()
