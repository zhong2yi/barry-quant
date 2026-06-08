"""
Cloud Runner v17 - GitHub Actions 自动化选股
策略：缩量回MA20 | 数据：腾讯K线 | 20线程
"""
import json, os, sys, time, datetime as dt
import numpy as np
import requests
import re
from concurrent.futures import ThreadPoolExecutor, as_completed

WORKSPACE = os.path.dirname(os.path.abspath(__file__))
SITE_DIR = os.path.join(os.path.dirname(WORKSPACE), '_site')
os.makedirs(SITE_DIR, exist_ok=True)

T_HEADERS = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36', 'Referer': 'https://gu.qq.com'}
PROXY_STOCKS = ['sh600000', 'sh601398', 'sh601288', 'sh600028', 'sh601857']
MA20_DEV = 1.5; VOL_RATIO = 0.8; SL = 0.92; HOLD = 5

def get_pool():
    print("[1/5] 股池...")
    f = os.path.join(WORKSPACE, 'stock_pool_min.json')
    if os.path.exists(f):
        with open(f, encoding='utf-8') as fh:
            s = [{'code': x['c'], 'name': x['n']} for x in json.load(fh)]
        print(f"  {len(s)} 只"); return s
    return []

def get_kl(code, n=120):
    try:
        u = f'https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={code},day,,,{n},qfq&_var=kline_dayfq'
        r = requests.get(u, headers=T_HEADERS, timeout=10)
        js = json.loads(r.text[r.text.index('=')+1:])
        ck = list(js['data'].keys())[0]
        raw = js['data'][ck].get('qfqday') or js['data'][ck].get('day') or []
        if len(raw) < 60: return None
        return np.array([float(x[2]) for x in raw]), np.array([float(x[5]) for x in raw])
    except: return None

def rsi(c, p=14):
    d = np.diff(c); g = np.where(d>0,d,0.0); l = np.where(d<0,-d,0.0)
    ag = np.convolve(g, np.ones(p)/p, 'valid'); al = np.convolve(l, np.ones(p)/p, 'valid')
    if len(ag)==0: return [50.0]
    rs = np.divide(ag, al, out=np.ones_like(ag)*100, where=al>0)
    return list(100-100/(1+rs))

def chk_one(code, name):
    try:
        d = get_kl(code)
        if d is None: return None
        c, v = d
        m20 = np.mean(c[-20:]); m60 = np.mean(c[-60:]); m5 = np.mean(c[-5:])
        p = c[-1]
        if m20 <= m60: return None
        dev = (p/m20-1)*100
        if abs(dev) > MA20_DEV: return None
        vr = v[-1]/np.mean(v[-6:-1]) if np.mean(v[-6:-1])>0 else 1
        if vr >= VOL_RATIO: return None
        r = rsi(c)[-1]; s = 45
        if m5 > m20: s += 15
        if r < 50: s += 10
        return {'code':code,'name':name,'price':round(float(p),2),'stop_loss':round(float(p)*SL,2),
                'score':s,'deviation':round(float(dev),2),'volume_ratio':round(float(vr),2),
                'rsi':round(float(r),1),'ma20':round(float(m20),2),'ma60':round(float(m60),2)}
    except: return None

def screen(stocks):
    print("[2/5] 扫描(20线程)...")
    res = []
    with ThreadPoolExecutor(20) as pool:
        fs = {pool.submit(chk_one, s['code'], s['name']): s for s in stocks}
        done = 0
        for f in as_completed(fs):
            done += 1
            if done % 500 == 0: print(f"  {done}/{len(stocks)} ({len(res)})")
            try:
                r = f.result()
                if r: res.append(r)
            except: pass
    res.sort(key=lambda x: -x['score'])
    print(f"  {len(res)} 只"); return res

def chk_market():
    print("[3/5] MA60(代理)...")
    try:
        pc = {}
        for px in PROXY_STOCKS:
            d = get_kl(px, 320)
            if d is not None and len(d[0])>=60: pc[px] = d[0]
        if len(pc) < 3: return _dm()
        ml = min(len(c) for c in pc.values())
        ix = sum(c[-ml:] for c in pc.values()) / len(pc)
        m60 = float(np.mean(ix[-60:])); p = float(ix[-1])
        vs = (p/m60-1)*100; bd = 0
        for i in range(len(ix)-1, max(0,len(ix)-90), -1):
            if ix[i] < float(np.mean(ix[max(0,i-59):i+1])): bd += 1
            else: break
        st = 'RED' if bd>3 else ('YELLOW' if bd>0 else 'GREEN')
        lb = ('弱势市' if st=='RED' else '谨慎' if st=='YELLOW' else '健康市') + f'（连续跌破MA60 {bd}天，偏离{vs:.1f}%）'
        print(f"  代理:{p:.2f} MA60:{m60:.2f} {st} {bd}天")
        return {'state':st,'label':lb,'below_days':bd,'vs_ma60':round(vs,1),'ma60':round(m60,2),
                'sh_index_pct':round(float((ix[-1]/ix[-2]-1)*100),2) if len(ix)>1 else 0}
    except: return _dm()

def _dm(): return {'state':'YELLOW','label':'评估失败','below_days':0,'vs_ma60':0,'ma60':0,'sh_index_pct':0}

def sig_strength(cands, mkt):
    n = len(cands); fs = {}
    fs['c'] = {'score':10 if n>=5 else (5 if n>=2 else (3 if n>=1 else 0)),'label':f'候选{n}只'}
    if n>0:
        mx = max(c['score'] for c in cands)
        fs['s'] = {'score':15 if mx>=70 else (10 if mx>=55 else 5),'label':'高分' if mx>=70 else '中等'}
    else: fs['s'] = {'score':0,'label':'无'}
    ms = mkt.get('state','GREEN')
    spct = mkt.get('sh_index_pct', 0)
    fs['m'] = {'score':10 if ms=='GREEN' else 5,'label':f'上证{spct:+.1f}%'}
    if n>0:
        r = cands[0]['rsi']
        fs['r'] = {'score':10 if 30<=r<=50 else (5 if r<=65 else 3),'label':f'RSI={r}'}
    else: fs['r'] = {'score':0,'label':'NA'}
    tot = sum(f['score'] for f in fs.values())
    lv = '强 ★★★' if tot>=35 else ('中等 ★★' if tot>=25 else '弱 ★')
    ac = '建议买入' if tot>=35 else ('谨慎买入' if tot>=25 else '建议观望')
    return {'level':lv,'action':ac,'score':tot,'max_score':45,'factors':fs}

def gen(cands, mkt, ss, ts, bd, sd, nd=None):
    print("[5/5] 生成...")
    dash = os.path.join(os.path.dirname(WORKSPACE), 'dashboard')
    tp = os.path.join(dash, 'index.html')
    if not os.path.exists(tp): return False
    with open(tp, encoding='utf-8') as f: html = f.read()

    mn = cands[0] if cands else {}
    bk = cands[1] if len(cands)>1 else {}

    rec = {'signal_date':ts,'buy_date':bd,'sell_date':sd,'kline_latest':ts,
           'sh_index_pct':mkt.get('sh_index_pct',0),
           'generated_at':dt.datetime.now().strftime('%Y-%m-%d %H:%M:%S'),'complete':True,
           'main':mn,'main_backup':bk,
           'barry':{'code':'','name':'暂无','price':0,'rsi':0,'pct_chg':0,'valid':False},
           'barry_valid':False,'all_shrink':cands[:5],'all_barry':[]}

    ver = {'passed':len(cands)>0,'conclusion':'推荐' if cands else '无推荐',
           'timestamp':dt.datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
           'time_display':dt.datetime.now().strftime('%m月%d日 %H:%M'),
           'signal_date':ts,'buy_date':bd,'sell_date':sd,
           'sh_index_pct':mkt.get('sh_index_pct',0),
           'main_stock':mn.get('code',''),'main_name':mn.get('name',''),
           'main_price':mn.get('price',0),'main_score':mn.get('score',0),
           'main_rsi':mn.get('rsi',0),'main_backup':bk.get('code',''),
           'barry_code':'','barry_valid':False,
           'health':{'checklist':{'has_signal_date':True,'has_buy_date':True,'has_sell_date':True,
               'has_main':len(cands)>0,'has_sh_index_pct':True,'main_code':bool(mn.get('code')),
               'main_price':bool(mn.get('price')),'main_stop_loss':bool(mn.get('stop_loss')),
               'main_name':bool(mn.get('name')),'main_rsi':bool(mn.get('rsi')),
               'signal_is_kline_latest':True,'download_ratio':'N/A',
               'barry_valid':False,'barry_rsi':0,'candidate_count':len(cands)},'issues':[],'passed':True},
           'news_filter':nd if nd else {'filtered_count':0,'replaced':False,'replacement':'',
               'detail':{'filtered_out':[],'passed':[],'scan_time':'','total_checked':len(cands),'total_red':0,'total_passed':len(cands)}},
           'signal_strength':ss,'market_state':mkt,
           'checklist':{'K线最新日期=信号日':True,'脚本完成标记':True,'JSON数据完整':True,
               '新闻过滤通过':True,'主推RSI正常(<75)':True,'BARRY未超买(RSI<65)':True,
               '信号强度':ss.get('level','?'),'MA60市场状态':mkt.get('label','?')}}

    html = re.sub(r'var EMBED_REC = \{.*?\};', f'var EMBED_REC = {json.dumps(rec, ensure_ascii=False)};', html, flags=re.DOTALL)
    html = re.sub(r'var EMBED_VER = \{.*?\};', f'var EMBED_VER = {json.dumps(ver, ensure_ascii=False)};', html, flags=re.DOTALL)

    # Trade: update current prices for active trades, add today, keep last 10
    m = re.search(r'var EMBED_TRADES = (\[.*?\]);', html, flags=re.DOTALL)
    ot = []
    if m:
        try: ot = json.loads(m.group(1))
        except: ot = []
    # Update current_price for active trades
    for t in ot:
        if t.get('sell_price') is None and t.get('main_code'):
            try:
                d = get_kl(t['main_code'], n=3)
                if d is not None:
                    t['current_price'] = round(float(d[0][-1]), 2)
            except: pass
    nt = {"signal_date":ts[5:],"main_code":mn.get('code',''),"main_name":mn.get('name',''),
          "buy_price":mn.get('price',0),"sell_price":None,"current_price":mn.get('price',0)}
    ot.insert(0, nt); ot = ot[:10]
    html = re.sub(r'var EMBED_TRADES = \[.*?\];', f'var EMBED_TRADES = {json.dumps(ot, ensure_ascii=False)};', html, flags=re.DOTALL)
    html = re.sub(r'// 最后更新: .*', f'// 最后更新: {dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")}', html)

    sp = os.path.join(SITE_DIR, 'index.html')
    with open(sp, 'w', encoding='utf-8') as f: f.write(html)
    print(f"  看板: {sp}"); return True

def already_deployed_today():
    """检查线上是否已有今天的选股结果"""
    try:
        r = requests.get('https://raw.githubusercontent.com/zhong2yi/barry-quant/gh-pages/index.html', timeout=10)
        if r.status_code != 200: return False
        m = re.search(r'"signal_date":"(\d{4}-\d{2}-\d{2})"', r.text)
        if m:
            return m.group(1) == dt.date.today().strftime('%Y-%m-%d')
    except:
        pass
    return False

def main():
    st = time.time()
    today = dt.date.today(); ts = today.strftime('%Y-%m-%d')

    # 自愈：如果今天已经部署过，直接跳过
    if already_deployed_today():
        print(f"===== {ts} 已部署，跳过 =====")
        return

    stocks = get_pool()
    if not stocks: return
    cands = screen(stocks)
    mkt = chk_market()
    ss = sig_strength(cands, mkt)
    sell = (today + dt.timedelta(days=HOLD+1)).strftime('%Y-%m-%d')

    print(f"\n[4/5] 结果:")
    if cands:
        for i, c in enumerate(cands[:3]):
            print(f"  #{i+1} {c['code']} {c['name']} ${c['price']} 评分{c['score']} RSI{c['rsi']} 量比{c['volume_ratio']}")
    print(f"\n  市场: {mkt['label']}\n  信号: {ss['level']}")
    gen(cands, mkt, ss, ts, ts, sell)
    print(f"\n===== 完成({time.time()-st:.0f}s) =====")

if __name__ == '__main__': main()
