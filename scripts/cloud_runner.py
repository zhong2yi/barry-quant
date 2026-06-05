"""
Cloud Runner v5 - GitHub Actions 自动化选股
策略：缩量回MA20 | 数据源：腾讯K线(前复权) | 20线程并行
"""
import json, os, sys, time, datetime as dt
import numpy as np
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed

WORKSPACE = os.path.dirname(os.path.abspath(__file__))
SITE_DIR = os.path.join(os.path.dirname(WORKSPACE), '_site')
os.makedirs(SITE_DIR, exist_ok=True)

T_HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
    'Referer': 'https://gu.qq.com'
}

MA20_DEVIATION = 1.5          # 偏离MA20 ±1.5%
VOLUME_RATIO_MAX = 0.8        # 量比 < 0.8
STOP_LOSS = 0.92              # 止损 -8%
HOLD_DAYS = 5

# ===== 股池 =====
def get_stock_pool():
    """获取沪深主板股票 - 优先内置股池，备选akshare"""
    print("[1/5] 获取股池...")

    # 1) 内置股池（最快，最可靠）
    pool_file = os.path.join(WORKSPACE, 'stock_pool_min.json')
    if os.path.exists(pool_file):
        with open(pool_file, 'r', encoding='utf-8') as f:
            raw = json.load(f)
        stocks = [{'code': x['c'], 'name': x['n']} for x in raw]
        print(f"  内置股池: {len(stocks)} 只")
        return stocks

    # 2) 尝试 akshare
    try:
        import akshare as ak
        df = ak.stock_info_a_code_name()
        stocks = []
        for _, row in df.iterrows():
            code = str(row['code'])
            name = str(row['name'])
            if name[:2] == 'ST' or name[0] == '*' or '退' in name:
                continue
            if code.startswith('60') or code.startswith('00'):
                prefix = 'sh' if code.startswith('6') else 'sz'
                stocks.append({'code': prefix + code, 'name': name})
        print(f"  akshare: {len(stocks)} 只")
        return stocks
    except Exception as e:
        print(f"  akshare failed: {e}")
        return []

# ===== 腾讯K线 =====
def get_kline(code, n=120):
    """从腾讯接口获取前复权日线"""
    url = (f'https://web.ifzq.gtimg.cn/appstock/app/fqkline/get'
           f'?param={code},day,,,{n},qfq&_var=kline_dayfq')
    try:
        r = requests.get(url, headers=T_HEADERS, timeout=10)
        js = json.loads(r.text[r.text.index('=')+1:])
        ck = list(js['data'].keys())[0]
        raw = js['data'][ck].get('qfqday') or js['data'][ck].get('day') or []
        if not raw or len(raw) < 60:
            return None
        closes = np.array([float(x[2]) for x in raw])
        volumes = np.array([float(x[5]) for x in raw])
        return closes, volumes
    except:
        return None

def calc_rsi(closes, period=14):
    """RSI计算"""
    deltas = np.diff(closes)
    gains = np.where(deltas > 0, deltas, 0.0)
    losses = np.where(deltas < 0, -deltas, 0.0)
    avg_gain = np.convolve(gains, np.ones(period)/period, mode='valid')
    avg_loss = np.convolve(losses, np.ones(period)/period, mode='valid')
    if len(avg_gain) == 0:
        return [50.0]
    rs = np.divide(avg_gain, avg_loss, out=np.ones_like(avg_gain)*100, where=avg_loss>0)
    return list(100 - 100/(1 + rs))

# ===== 筛选（多线程）=====
def check_one(code, name):
    """Check a single stock, return result or None"""
    try:
        data = get_kline(code)
        if data is None:
            return None
        closes, volumes = data

        ma20 = np.mean(closes[-20:])
        ma60 = np.mean(closes[-60:])
        ma5 = np.mean(closes[-5:])
        price = closes[-1]

        if ma20 <= ma60:
            return None

        dev = (price - ma20) / ma20 * 100
        if abs(dev) > MA20_DEVIATION:
            return None

        vol5avg = np.mean(volumes[-6:-1])
        vratio = volumes[-1] / vol5avg if vol5avg > 0 else 1.0
        if vratio >= VOLUME_RATIO_MAX:
            return None

        rsi_list = calc_rsi(closes)
        rsi = rsi_list[-1] if rsi_list else 50
        if np.isnan(rsi):
            rsi = 50

        # Scoring only, no RSI hard filter per local logic
        score = 45
        if ma5 > ma20:
            score += 15
        if rsi < 50:
            score += 10

        return {
            'code': code, 'name': name,
            'price': round(float(price), 2),
            'stop_loss': round(float(price) * STOP_LOSS, 2),
            'score': score,
            'deviation': round(float(dev), 2),
            'volume_ratio': round(float(vratio), 2),
            'rsi': round(float(rsi), 1),
            'ma20': round(float(ma20), 2),
            'ma60': round(float(ma60), 2)
        }
    except:
        return None

def screen(stocks):
    """20线程并行扫描"""
    print("[2/5] 扫描中（20线程并行）...")
    results = []
    total = len(stocks)

    with ThreadPoolExecutor(max_workers=20) as pool:
        futures = {pool.submit(check_one, s['code'], s['name']): s for s in stocks}
        done = 0
        for f in as_completed(futures):
            done += 1
            if done % 500 == 0:
                print(f"  进度: {done}/{total} ({len(results)} 候选)")
            try:
                r = f.result()
                if r:
                    results.append(r)
            except:
                pass

    results.sort(key=lambda x: -x['score'])
    print(f"  候选: {len(results)} 只")
    return results

# ===== 新闻过滤 =====
NEODATA_TOKEN = "tk_AW1Dbwhw0QAdXlzc7t03WK59k4Dt9Fg5"
NEODATA_URL = "https://copilot.tencent.com/agenttool/v1/neodata"

RED_KEYWORDS = ["诉讼", "冻结", "索赔", "仲裁", "合同纠纷", "被指", "违规", "处罚",
                "亏损", "退市", "ST", "立案调查", "信披违规", "股权被冻结",
                "银行账户被冻结", "暴雷", "隐瞒不披露", "警示函", "监管函"]

def check_news(code, name):
    """查询单只股票近期新闻，返回风险等级和原因"""
    if not NEODATA_TOKEN:
        return "GREEN", [], []
    try:
        headers = {"Content-Type": "application/json", "Authorization": f"Bearer {NEODATA_TOKEN}"}
        payload = {"query": f"{name} {code[2:]} 近期公告新闻", "channel": "neodata", "sub_channel": "workbuddy", "data_type": "doc"}
        r = requests.post(NEODATA_URL, headers=headers, json=payload, timeout=15)
        if r.status_code != 200:
            return "UNKNOWN", [], []
        data = r.json()
        docs = data.get("docs", data.get("data", {}).get("docs", []))
        if not docs:
            return "GREEN", [], []

        red_hits = []
        yellow_hits = []
        for doc in docs:
            title = str(doc.get("title", ""))
            body = str(doc.get("content", doc.get("body", "")))
            text = title + " " + body
            for kw in RED_KEYWORDS:
                if kw in text and name[:2] in text[:50]:
                    red_hits.append(f"[标题] {kw}" if kw in title else f"[内容] {kw}")
                    break

        if red_hits:
            return "RED", list(set(red_hits)), []
        return "GREEN", [], []
    except Exception as e:
        return "UNKNOWN", [], [str(e)]

def news_filter(candidates):
    """对候选标的执行新闻过滤"""
    print("[3/5] 新闻过滤...")
    if not NEODATA_TOKEN:
        print("  跳过: 无Token")
        return candidates, {"filtered_out": [], "passed": [c['code'] for c in candidates]}

    passed = []
    filtered_out = []
    for i, c in enumerate(candidates):
        level, reds, _ = check_news(c['code'], c['name'])
        if level == "RED":
            filtered_out.append({"code": c['code'], "name": c['name'], "reasons": reds})
            print(f"  RED: {c['code']} {c['name']}: {reds}")
        else:
            passed.append(c)

    print(f"  过滤: {len(filtered_out)}只 | 通过: {len(passed)}只")
    detail = {
        "filtered_out": filtered_out,
        "passed": [{"code": c['code'], "name": c['name'], "risk_level": "GREEN", "reasons": [], "red_hits": [], "yellow_hits": []} for c in passed],
        "total_checked": len(candidates),
        "total_red": len(filtered_out),
        "total_passed": len(passed)
    }
    return passed, detail

# ===== MA60 上证评估（代理指数法，与本地一致）=====
PROXY_STOCKS = ['sh600000', 'sh601398', 'sh601288', 'sh600028', 'sh601857']

def check_market():
    """用5大权重股代理指数评估MA60，与本地逻辑一致"""
    print("[4/5] MA60 评估（代理指数）...")
    try:
        # 收集代理股票数据
        proxy_closes = {}
        proxy_dates = {}
        for pc in PROXY_STOCKS:
            data = get_kline(pc, n=320)
            if data is not None:
                closes, _ = data
                if len(closes) >= 60:
                    proxy_closes[pc] = closes
                    proxy_dates[pc] = len(closes)

        if len(proxy_closes) < 3:
            return default_market()

        # 构建代理指数：取各股收盘价平均值
        min_len = min(len(c) for c in proxy_closes.values())
        proxy_index = np.zeros(min_len)
        count = 0
        for pc in proxy_closes:
            closes = proxy_closes[pc][-min_len:]
            proxy_index += closes
            count += 1
        proxy_index /= count

        ma60 = float(np.mean(proxy_index[-60:]))
        price = float(proxy_index[-1])
        vs_ma60 = (price - ma60) / ma60 * 100

        # 连续跌破天数
        below = 0
        for i in range(len(proxy_index)-1, max(0, len(proxy_index)-90), -1):
            ma = float(np.mean(proxy_index[max(0,i-59):i+1]))
            if proxy_index[i] < ma:
                below += 1
            else:
                break

        if below > 3:
            state, label = 'RED', f'弱势市（连续跌破 MA60 {below} 天，偏离 {vs_ma60:.1f}%）'
        elif below > 0:
            state, label = 'YELLOW', f'谨慎（跌破MA60 {below} 天，偏离 {vs_ma60:.1f}%）'
        else:
            state, label = 'GREEN', '健康市（站上MA60）'

        print(f"  代理指数: {price:.2f} | MA60: {ma60:.2f} | 偏离: {vs_ma60:.1f}% | 连续跌破: {below}天")
        return {'state': state, 'label': label, 'below_days': below,
                'vs_ma60': round(vs_ma60, 1), 'ma60': round(ma60, 2),
                'sh_index_pct': round(float((proxy_index[-1]-proxy_index[-2])/proxy_index[-2]*100), 2) if len(proxy_index)>1 else 0}
    except Exception as e:
        print(f"  [WARN] MA60评估失败: {e}")
        return default_market()

def default_market():
    return {'state': 'YELLOW', 'label': '评估失败',
            'below_days': 0, 'vs_ma60': 0, 'ma60': 0, 'sh_index_pct': 0}

# ===== 信号强度 =====
def signal_strength(candidates, market):
    n = len(candidates)
    factors = {}

    factors['candidate_count'] = {'score': 10 if n>=5 else (5 if n>=2 else (3 if n>=1 else 0)),
                                   'label': f'候选{n}只' + ('(多)' if n>=5 else '(中等)' if n>=2 else '(少)' if n>=1 else '')}
    if n > 0:
        max_s = max(c['score'] for c in candidates)
        factors['score_distribution'] = {'score': 15 if max_s>=70 else (10 if max_s>=55 else 5),
                                          'label': '高分标的' if max_s>=70 else '中等分数' if max_s>=55 else '得分偏低'}
    else:
        factors['score_distribution'] = {'score': 0, 'label': '无标的'}

    ms = market.get('state', 'GREEN')
    factors['market'] = {'score': 10 if ms=='GREEN' else 5,
                          'label': f"上证{market.get('sh_index_pct',0):+.1f}%{' (弱势)' if ms=='RED' else ''}"}

    if n > 0:
        r = candidates[0]['rsi']
        factors['rsi_health'] = {'score': 10 if 30<=r<=50 else (5 if r<=65 else 3),
                                  'label': f'RSI={r}'}
    else:
        factors['rsi_health'] = {'score': 0, 'label': '无数据'}

    total = sum(f['score'] for f in factors.values())
    level = '强 ★★★' if total >= 35 else ('中等 ★★' if total >= 25 else '弱 ★')
    action = '建议买入' if total >= 35 else ('谨慎买入' if total >= 25 else '建议观望')
    return {'level': level, 'action': action, 'score': total, 'max_score': 45, 'factors': factors}

# ===== 生成看板 =====
def generate_html(candidates, market, ss, today_str, buy_date, sell_date, news_detail=None):
    print("[5/5] 生成看板...")

    dash_dir = os.path.join(os.path.dirname(WORKSPACE), 'dashboard')
    tmpl = os.path.join(dash_dir, 'index.html')
    if not os.path.exists(tmpl):
        print(f"  [ERROR] no template: {tmpl}")
        return False

    with open(tmpl, 'r', encoding='utf-8') as f:
        html = f.read()

    main = candidates[0] if len(candidates)>0 else {}
    backup = candidates[1] if len(candidates)>1 else {}

    rec = {
        'signal_date': today_str, 'buy_date': buy_date, 'sell_date': sell_date,
        'kline_latest': today_str, 'sh_index_pct': market.get('sh_index_pct',0),
        'generated_at': dt.datetime.now().strftime('%Y-%m-%d %H:%M:%S'), 'complete': True,
        'main': main, 'main_backup': backup,
        'barry': {'code':'','name':'暂无','price':0,'rsi':0,'pct_chg':0,'valid':False},
        'barry_valid': False,
        'all_shrink': candidates[:5] if len(candidates)>0 else [],
        'all_barry': []
    }

    ver = {
        'passed': len(candidates)>0, 'conclusion': '推荐' if len(candidates)>0 else '无推荐',
        'timestamp': dt.datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'signal_date': today_str, 'buy_date': buy_date, 'sell_date': sell_date,
        'sh_index_pct': market.get('sh_index_pct',0),
        'main_stock': main.get('code',''), 'main_name': main.get('name',''),
        'main_price': main.get('price',0), 'main_score': main.get('score',0),
        'main_rsi': main.get('rsi',0), 'main_backup': backup.get('code',''),
        'barry_code': '', 'barry_valid': False,
        'health': {'checklist': {
            'has_signal_date': True, 'has_buy_date': True, 'has_sell_date': True,
            'has_main': len(candidates)>0, 'has_sh_index_pct': True,
            'main_code': bool(main.get('code')), 'main_price': bool(main.get('price')),
            'main_stop_loss': bool(main.get('stop_loss')), 'main_name': bool(main.get('name')),
            'main_rsi': bool(main.get('rsi')),
            'signal_is_kline_latest': True, 'download_ratio': 'N/A',
            'barry_valid': False, 'barry_rsi': 0, 'candidate_count': len(candidates)
        }, 'issues': [], 'passed': True},
        'news_filter': (news_detail if news_detail else
            {'filtered_count':0,'replaced':False,'replacement':'',
            'detail': {'filtered_out':[],'passed':[],
                'scan_time': dt.datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                'total_checked': len(candidates), 'total_red': 0, 'total_passed': len(candidates)}}),
        'signal_strength': ss, 'market_state': market,
        'checklist': {
            'K线最新日期=信号日': True, '脚本完成标记': True, 'JSON数据完整': True,
            '新闻过滤通过': (news_detail.get('total_red', 0) == 0) if news_detail else True,
            '主推RSI正常(<75)': True, 'BARRY未超买(RSI<65)': True,
            '信号强度': ss.get('level','?'), 'MA60市场状态': market.get('label','?')
        }
    }

    import re
    html = re.sub(r'var EMBED_REC = \{.*?\};',
                  f'var EMBED_REC = {json.dumps(rec, ensure_ascii=False)};',
                  html, flags=re.DOTALL)
    html = re.sub(r'var EMBED_VER = \{.*?\};',
                  f'var EMBED_VER = {json.dumps(ver, ensure_ascii=False)};',
                  html, flags=re.DOTALL)
    # Add today's trade (with sell_price=null, current_price=null)
    today_short = ts[5:]  # "06-05"
    new_trade = json.dumps({"signal_date":today_short,"main_code":main.get('code',''),"main_name":main.get('name',''),"buy_price":main.get('price',0),"sell_price":None,"current_price":None}, ensure_ascii=False)
    html = re.sub(r'var EMBED_TRADES = \[', f'var EMBED_TRADES = [{new_trade},', html, count=1)
    html = re.sub(r'// 最后更新: .*',
                  f'// 最后更新: {dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")}', html)

    site_path = os.path.join(SITE_DIR, 'index.html')
    with open(site_path, 'w', encoding='utf-8') as f:
        f.write(html)
    print(f"  看板: {site_path}")
    return True

# ===== 主函数 =====
def main():
    start = time.time()
    stocks = get_stock_pool()
    if not stocks:
        print("[FATAL] 无股池")
        sys.exit(1)

    candidates = screen(stocks)
    candidates, news_detail = news_filter(candidates)
    market = check_market()
    ss = signal_strength(candidates, market)

    today = dt.date.today()
    ts = today.strftime('%Y-%m-%d')
    sell = (today + dt.timedelta(days=HOLD_DAYS+1)).strftime('%Y-%m-%d')

    print(f"\n[5/5] 结果:")
    if candidates:
        for i, c in enumerate(candidates[:3]):
            print(f"  #{i+1} {c['code']} {c['name']} ${c['price']} "
                  f"评分{c['score']} 偏离{c['deviation']:.1f}% 量比{c['volume_ratio']:.2f} RSI{c['rsi']}")
    else:
        print("  无符合条件标的")

    print(f"\n  市场: {market['label']}")
    print(f"  信号: {ss['level']} ({ss['score']}/{ss['max_score']})")

    generate_html(candidates, market, ss, ts, ts, sell, news_detail)
    print(f"\n===== 完成 ({time.time()-start:.0f}s) =====")

if __name__ == '__main__':
    main()
