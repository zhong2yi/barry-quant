"""
============================================================
★★★ 统一推荐入口 — run_before_recommend.py ★★★
============================================================
唯一入口脚本，用户说"今日标的"时只运行此脚本。
包含：选股→验证→新闻过滤→信号强度→输出结论

输出文件:
  latest_recommendation.json  — 选股结果
  news_filter_result.json     — 新闻过滤结果
  verification.json           — 最终验证+信号强度+检查清单
============================================================

硬规则:
  1. signal_date 必须 = K线实际最新日期
  2. 不完整输出绝不拼凑推荐
  3. BARRY策略 barry_valid=false 时不推荐
  4. news_filter 非阻塞（WARN不FAIL）
"""

import json, os, sys, subprocess, datetime as dt

if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', line_buffering=True)

WORKSPACE = os.path.dirname(os.path.abspath(__file__))
JSON_PATH = os.path.join(WORKSPACE, "latest_recommendation.json")
VERIFY_PATH = os.path.join(WORKSPACE, "verification.json")
NEWS_FILTER_PATH = os.path.join(WORKSPACE, "news_filter_result.json")
PYTHON = sys.executable

# ============================================================
# Step 1: 运行选股脚本
# ============================================================
def run_screener():
    script = os.path.join(WORKSPACE, "cloud_quant", "scripts", "cloud_runner.py")
    print("=" * 60)
    print("[Step 1/5] 运行选股脚本(v2.1)...")
    print("=" * 60)
    try:
        result = subprocess.run([PYTHON, "-u", script, "--force"], capture_output=True, timeout=600, cwd=WORKSPACE)
        print(result.stdout.decode('utf-8', errors='replace'))

        if not os.path.exists(JSON_PATH):
            print("[Step 1/5] FAIL - JSON未生成")
            return False, "JSON not generated"

        with open(JSON_PATH, 'r', encoding='utf-8') as f:
            data = json.load(f)

        if not data.get("signal_date") or not data.get("main"):
            print("[Step 1/5] FAIL - JSON不完整（缺signal_date或main）")
            return False, "JSON incomplete"

        print(f"[Step 1/5] PASS - 选股完成")
        return True, "OK"
    except subprocess.TimeoutExpired:
        print("[Step 1/5] FAIL - 超时(>10分钟)")
        return False, "Timeout"
    except Exception as e:
        print(f"[Step 1/5] FAIL - {e}")
        return False, str(e)


# ============================================================
# Step 2: JSON验证 + 数据健康检查
# ============================================================
def verify_and_healthcheck():
    print(f"\n{'=' * 60}")
    print("[Step 2/5] JSON验证 + 数据健康检查")
    print("=" * 60)

    try:
        with open(JSON_PATH, 'r', encoding='utf-8') as f:
            data = json.load(f)
    except Exception as e:
        return False, data, {"json_error": str(e)}

    checks = {}
    issues = []

    # 2a. 基本字段完整性
    for key in ["signal_date", "buy_date", "sell_date", "main", "sh_index_pct"]:
        checks[f"has_{key}"] = key in data and data[key] is not None
        if not checks[f"has_{key}"]:
            issues.append(f"缺少字段: {key}")

    # 2b. main标的完整性
    main = data.get("main", {})
    for key in ["code", "price", "stop_loss", "name", "rsi"]:
        checks[f"main_{key}"] = key in main and main[key] is not None
        if not checks[f"main_{key}"]:
            issues.append(f"main缺失: {key}")
    if main.get("price", 0) <= 0:
        issues.append(f"main价格无效: {main.get('price')}")

    # 2c. 数据时效验证
    signal_date = data.get("signal_date", "")
    kline_latest = data.get("kline_latest", signal_date)
    checks["signal_is_kline_latest"] = signal_date == kline_latest
    if signal_date != kline_latest:
        issues.append(f"信号日({signal_date}) != K线最新({kline_latest})！数据过期！")

    # 2d. 下载成功率
    total_stocks = data.get("total_stocks", 0)
    downloaded = data.get("downloaded", 0)
    if total_stocks > 0:
        ratio = downloaded / total_stocks * 100
        checks["download_ratio"] = round(ratio, 1)
        if ratio < 50:
            issues.append(f"下载率过低: {downloaded}/{total_stocks} ({ratio:.0f}%)")
    else:
        checks["download_ratio"] = "N/A"

    # 2e. BARRY检查
    barry = data.get("barry", {})
    barry_valid = data.get("barry_valid", True)
    checks["barry_valid"] = barry_valid
    checks["barry_rsi"] = barry.get("rsi", "N/A")

    # 2f. 股票池大小
    all_shrink = data.get("all_shrink", [])
    checks["candidate_count"] = len(all_shrink)

    passed = len(issues) == 0
    if passed:
        print("[Step 2/5] PASS - 数据健康")

    health = {
        "checklist": checks,
        "issues": issues,
        "passed": passed,
    }

    for k, v in checks.items():
        status = "OK" if (isinstance(v, bool) and v) else str(v)
        label = {"has_signal_date": "信号日", "has_buy_date": "买入日",
                 "has_sell_date": "卖出日", "has_main": "主推存在",
                 "has_sh_index_pct": "上证涨跌",
                 "signal_is_kline_latest": "信号日=K线最新",
                 "download_ratio": "下载率%",
                 "barry_valid": "BARRY可用",
                 "barry_rsi": "BARRY-RSI",
                 "candidate_count": "候选数",
                 "main_code": "主推代码", "main_price": "主推价格",
                 "main_stop_loss": "止损价", "main_name": "主推名称",
                 "main_rsi": "主推RSI"}.get(k, k)
        if passed or (isinstance(v, bool) and v):
            print(f"  OK  {label}: {status}")
        else:
            print(f"  !!  {label}: {status}")

    if issues:
        print(f"\n  [警告] {', '.join(issues)}")

    return passed, data, health


# ============================================================
# Step 3: 新闻过滤（方向1）
# ============================================================
def run_news_filter(data):
    print(f"\n{'=' * 60}")
    print("[Step 3/5] 新闻/公告利空过滤")
    print("=" * 60)

    filter_script = os.path.join(WORKSPACE, "news_filter.py")
    if not os.path.exists(filter_script):
        print("[Step 3/5] SKIP - news_filter.py不存在")
        return True, data, {"status": "SKIP", "reason": "Script missing"}

    # 收集候选代码
    codes = set()
    if data.get("main"):
        codes.add(data["main"]["code"])
    if data.get("main_backup"):
        codes.add(data["main_backup"]["code"])
    for item in data.get("all_shrink", []):
        codes.add(item["code"])

    if not codes:
        print("[Step 3/5] SKIP - 无候选代码")
        return True, data, {"status": "SKIP", "reason": "No candidates"}

    cmd = [PYTHON, "-X", "utf8", filter_script, "--codes", ",".join(list(codes))]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=120, cwd=WORKSPACE)
    except subprocess.TimeoutExpired:
        print("[Step 3/5] WARN - 超时，跳过")
        return True, data, {"status": "TIMEOUT"}
    except Exception as e:
        print(f"[Step 3/5] WARN - {e}，跳过")
        return True, data, {"status": "ERROR", "error": str(e)}

    # 读取过滤结果
    try:
        with open(NEWS_FILTER_PATH, 'r', encoding='utf-8') as f:
            filter_result = json.load(f)
    except:
        print("[Step 3/5] WARN - 无法读取结果，跳过")
        return True, data, {"status": "ERROR", "error": "Result unreadable"}

    red_codes = [item["code"] for item in filter_result.get("filtered_out", [])]
    passed_codes = filter_result.get("passed", [])

    if not red_codes:
        print(f"[Step 3/5] PASS - 无RED标的")
        return True, data, {"status": "OK", "filtered": 0, "detail": filter_result}

    print(f"  RED: {len(red_codes)}只")
    for fc in filter_result.get("filtered_out", []):
        print(f"    {fc['code']} {fc['name']}: {'; '.join(fc['reasons'])}")

    # 主推被过滤 → 替换
    main = data.get("main", {})
    main_backup = data.get("main_backup", {})
    replaced = False
    replacement_info = ""

    if main.get("code") in red_codes:
        print(f"  >> 主推 {main['code']} 被过滤! 寻找替代...")
        if main_backup.get("code") and main_backup["code"] not in red_codes:
            data["main"] = main_backup
            data["main_backup"] = None
            replaced = True
            replacement_info = f"备选 {main_backup['code']} → 主推"
            print(f"    替代: {replacement_info}")
        else:
            for p in passed_codes:
                if p["code"] not in red_codes and p["code"] != main.get("code"):
                    for item in data.get("all_shrink", []):
                        if item["code"] == p["code"]:
                            data["main"] = item
                            replaced = True
                            replacement_info = f"候选 {item['code']} → 主推"
                            print(f"    替代: {replacement_info}")
                            break
                    if replaced:
                        break

    if main_backup and main_backup.get("code") in red_codes:
        new_backup = None
        for p in passed_codes:
            if p["code"] not in red_codes and p["code"] != data["main"]["code"]:
                for item in data.get("all_shrink", []):
                    if item["code"] == p["code"]:
                        new_backup = item
                        break
                if new_backup:
                    break
        data["main_backup"] = new_backup

    with open(JSON_PATH, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    print(f"[Step 3/5] PASS - 过滤完成 (过滤{len(red_codes)}只, 替换主推:{replaced})")
    return True, data, {
        "status": "OK",
        "filtered": len(red_codes),
        "replaced": replaced,
        "replacement": replacement_info,
        "detail": filter_result,
    }


# ============================================================
# Step 3.5: MA60 市场状态评估（三级市场过滤）
# ============================================================
def check_market_state():
    """用上证指数判断市场状态（Sina API优先，云选股结果兜底）"""
    print(f"\n{'=' * 60}")
    print("[Step 3.5/5] MA60 市场状态评估")
    print("=" * 60)

    try:
        import requests, numpy as np
        
        # 1. Sina K线（数据最新、最快）
        closes = None
        try:
            u = 'https://quotes.sina.cn/cn/api/json_v2.php/CN_MarketData.getKLineData?symbol=sh000001&scale=240&datalen=320'
            r = requests.get(u, headers={'User-Agent':'Mozilla/5.0','Referer':'https://finance.sina.com.cn'}, timeout=10)
            if r.status_code == 200 and len(r.text) > 50:
                raw = json.loads(r.text)
                if len(raw) >= 60:
                    closes = np.array([float(x['close']) for x in raw])
        except: pass
        
        if closes is None:
            # 2. Sina实时+云选股MA60
            try:
                u2 = 'https://hq.sinajs.cn/list=sh000001'
                r2 = requests.get(u2, headers={'Referer':'https://finance.sina.com.cn'}, timeout=5)
                if r2.status_code == 200:
                    parts = r2.text.split('"')[1].split(',')
                    p = float(parts[3]) if float(parts[3]) > 0 else float(parts[1])
                    rec = json.load(open('latest_recommendation.json','r',encoding='utf-8'))
                    m60 = rec.get('all_shrink',[])[0].get('ma60',0) if rec.get('all_shrink') else 0
                    if m60 > 0 and p > 0:
                        vs = (p/m60-1)*100
                        state = "GREEN" if vs > 0 else ("YELLOW" if vs > -1 else "RED")
                        level = {"GREEN":"🟢","YELLOW":"🟡","RED":"🔴"}[state]
                        label = f"健康市（Sina实时上证{p:.0f}，MA60={m60:.0f}，偏离{vs:+.1f}%）" if vs>0 else f"弱势市（偏离{vs:.1f}%）"
                        print(f"  Sina实时上证:{p:.2f} | 云计算MA60:{m60:.2f} | {state}")
                        return {"state":state,"level":level,"label":label,"below_days":0 if vs>0 else 1,"vs_ma60":round(vs,1),"ma60":round(m60,2)}
            except: pass
            print("  WARN - 在线数据获取失败")
            return {"state": "YELLOW", "level": "🟡", "label": "数据获取失败", "below_days": 0}
        
        p = float(closes[-1])
        m60 = float(np.mean(closes[-60:]))
        vs = (p/m60-1)*100
        bd = 0
        for i in range(len(closes)-1, max(0, len(closes)-90), -1):
            if closes[i] < float(np.mean(closes[max(0,i-59):i+1])): bd += 1
            else: break
        
        if bd > 3:
            state, level = "RED", "🔴"
            label = f"弱势市（连续跌破MA60 {bd}天，偏离{vs:.1f}%）"
        elif bd > 0:
            state, level = "YELLOW", "🟡"
            label = f"谨慎市（跌破MA60 {bd}天，偏离{vs:.1f}%）"
        else:
            state, level = "GREEN", "🟢"
            label = f"健康市（价格>MA60，偏离{vs:+.1f}%）"
        
        print(f"  上证(Sina K线):{p:.2f}  |  MA60:{m60:.2f}  |  偏离:{vs:+.1f}%  |  {state} {bd}天")
        if state == "RED":
            print(f"  ⚠️  回测：弱势市胜率仅 25%，建议观望")
        
        return {"state": state, "level": level, "label": label,
                "below_days": bd, "vs_ma60": round(vs, 1), "ma60": round(float(m60), 2)}
    except Exception as e:
        print(f"  WARN - 计算失败: {e}")
        return {"state": "YELLOW", "level": "🟡", "label": f"计算失败: {e}", "below_days": 0}


# ============================================================
# Step 4: 信号强度评估
# ============================================================
def assess_signal_strength(data):
    print(f"\n{'=' * 60}")
    print("[Step 4/5] 信号强度评估")
    print("=" * 60)

    all_shrink = data.get("all_shrink", [])
    sh_pct = data.get("sh_index_pct", 0)
    main = data.get("main", {})

    # 因子1：候选数量
    n = len(all_shrink)
    if n >= 4:
        n_score = 10
        n_label = f"候选{n}只(多)"
    elif n >= 2:
        n_score = 6
        n_label = f"候选{n}只(中等)"
    else:
        n_score = 3
        n_label = f"候选{n}只(少)"

    # 因子2：得分分布
    scores = [item["score"] for item in all_shrink if "score" in item]
    score_score = 5
    score_label = "得分分布正常"
    if scores and len(scores) >= 2 and scores[0] >= 60:
        if scores[0] - scores[1] >= 15:
            score_score = 15
            score_label = f"Top1({scores[0]})远超Top2({scores[1]})"
    if main.get("score", 0) < 60:
        score_score = 0
        score_label = f"Top1得分{main.get('score')}偏低"

    # 因子3：大盘环境
    if sh_pct > 1.0:
        mkt_score = 10
        mkt_label = f"上证{sh_pct:+.2f}%(强势)"
    elif sh_pct > 0:
        mkt_score = 7
        mkt_label = f"上证{sh_pct:+.2f}%(平稳)"
    elif sh_pct > -1.0:
        mkt_score = 5
        mkt_label = f"上证{sh_pct:+.2f}%(偏弱)"
    else:
        mkt_score = 0
        mkt_label = f"上证{sh_pct:+.2f}%(弱势!)"

    # 因子4：RSI健康度
    main_rsi = main.get("rsi", 50)
    if 40 <= main_rsi <= 55:
        rsi_score = 10
    elif 35 <= main_rsi <= 60:
        rsi_score = 7
    else:
        rsi_score = 4

    # 因子5：资金流向（量价代理，沙箱无真实主力净流入源）
    fund_score_val = main.get("fund_score", 0)
    if main.get("fund_proxy", False):
        fund_label = "资金代理(量价)"
    elif isinstance(main.get("fund_main"), (int, float)):
        fund_label = f"主力净流{main['fund_main']:.0f}"
    else:
        fund_label = "资金N/A"

    # 因子6：Alpha158综合评分（新增）
    alpha_total = main.get("alpha_total", 0)
    alpha_score = min(10, max(0, int(alpha_total/10) - 3))  # 30分以下=0, 70分以上=4, 100分=7

    total = n_score + score_score + mkt_score + rsi_score + fund_score_val + alpha_score

    if total >= 40:
        level = "强信号 ★★★"
        action = "推荐买入"
    elif total >= 25:
        level = "中等 ★★"
        action = "谨慎买入"
    else:
        level = "弱信号 ★"
        action = "建议观望"

    print(f"  候选数: {n_score}分 | 得分分布: {score_score}分 | 大盘: {mkt_score}分 | RSI: {rsi_score}分 | 资金: {fund_score_val}分 | Alpha: {alpha_score}分")
    print(f"  总分: {total}/65 → {level} ({action})")

    return {
        "level": level,
        "action": action,
        "score": total,
        "max_score": 65,
        "factors": {
            "candidate_count": {"score": n_score, "label": n_label},
            "score_distribution": {"score": score_score, "label": score_label},
            "market": {"score": mkt_score, "label": mkt_label},
            "rsi_health": {"score": rsi_score, "label": f"RSI={main_rsi}"},
            "fund_flow": {"score": fund_score_val, "label": fund_label},
            "alpha158": {"score": alpha_score, "label": f"Alpha{alpha_total:.0f}"},
        },
    }


# ============================================================
# Step 5: 输出最终结论 + 检查清单
# ============================================================
def write_final_output(passed, data, health, news_result, signal_strength, market_state=None):
    print(f"\n{'=' * 60}")
    print("[Step 5/5] 输出最终结论")
    print("=" * 60)

    main = data.get("main", {}) if data else {}
    barry = data.get("barry", {}) if data else {}
    main_backup = data.get("main_backup", {})

    # 交易清单
    checklist = {
        "K线最新日期=信号日": health.get("checklist", {}).get("signal_is_kline_latest", "?"),
        "脚本完成标记": data.get("complete", False) if data else False,
        "JSON数据完整": len(health.get("issues", [])) == 0,
        "新闻过滤通过": news_result.get("status") == "OK" or news_result.get("filtered", 0) == 0,
        "主推RSI正常(<75)": main.get("rsi", 0) < 75,
        "BARRY未超买(RSI<65)": data.get("barry_valid", False) if data else False,
        "信号强度": signal_strength.get("level", "?"),
    }

    # MA60 市场状态提示
    if market_state and market_state.get("state") == "RED":
        print(f"\n  {'='*50}")
        print(f"  ⚠️  MA60 市场过滤：{market_state['level']} {market_state['label']}")
        print(f"  ⚠️  回测数据：弱势市胜率仅 25%，累计净值 -53%")
        print(f"  ⚠️  建议：谨慎操作或空仓观望")
        print(f"  {'='*50}")
        checklist["MA60市场状态"] = f"🔴 {market_state['label']}"
    elif market_state and market_state.get("state") == "YELLOW":
        checklist["MA60市场状态"] = f"🟡 {market_state['label']}"
    elif market_state:
        checklist["MA60市场状态"] = f"🟢 {market_state.get('label','正常')}"
    else:
        checklist["MA60市场状态"] = "数据不足"
    all_checks_ok = all(
        v == True or v == "强信号 ★★★" or v == "中等 ★★"
        for v in checklist.values()
    ) if data else False

    # 结论
    if not passed or not data:
        conclusion = "不推荐 - 脚本异常"
    elif not all_checks_ok:
        conclusion = "谨慎 - 部分检查未通过"
    elif signal_strength.get("action") == "建议观望":
        conclusion = "观望 - 信号偏弱"
    else:
        conclusion = "推荐 - 全部检查通过"

    print(f"\n  结论: {conclusion}")
    print(f"\n  信号日: {data.get('signal_date')} | 买入日: {data.get('buy_date')} | 卖出日: {data.get('sell_date')}")
    print(f"  上证: {data.get('sh_index_pct', 0):+.2f}%")
    if main:
        print(f"\n  主推: {main['code']} {main.get('name','')} @ {main['price']} (止损{main['stop_loss']})")
        print(f"    得分: {main.get('score','?')} | RSI: {main.get('rsi','?')} | 偏离MA20: {main.get('dev','?')}%")
        if main_backup:
            print(f"  备选: {main_backup.get('code','')} {main_backup.get('name','')} @ {main_backup.get('price','?')}")
    if barry and data.get("barry_valid"):
        print(f"\n  BARRY: {barry['code']} {barry.get('name','')} @ {barry['price']} (RSI{barry.get('rsi','')})")
    elif barry:
        print(f"\n  BARRY: {barry.get('code','')} RSI{barry.get('rsi','')}超买, 跳过")

    # 检查清单
    print(f"\n  ┌{'检查清单':─^40}┐")
    for label, val in checklist.items():
        icon = "OK" if val else "!!"
        print(f"  | {icon}  {label:<35} |")
    print(f"  └{'─' * 40}┘")

    # 写verification.json
    now = dt.datetime.now()
    verify_result = {
        "passed": passed and all_checks_ok,
        "conclusion": conclusion,
        "timestamp": now.strftime('%Y-%m-%d %H:%M:%S'),
        "time_display": now.strftime('%m月%d日 %H:%M'),
        "signal_date": data.get("signal_date") if data else None,
        "buy_date": data.get("buy_date") if data else None,
        "sell_date": data.get("sell_date") if data else None,
        "sh_index_pct": data.get("sh_index_pct") if data else None,
        "main_stock": main.get("code") if main else None,
        "main_name": main.get("name") if main else None,
        "main_price": main.get("price") if main else None,
        "main_score": main.get("score") if main else None,
        "main_rsi": main.get("rsi") if main else None,
        "main_backup": main_backup.get("code") if main_backup else None,
        "barry_code": barry.get("code") if barry else None,
        "barry_valid": data.get("barry_valid") if data else None,
        "health": health,
        "news_filter": {
            "filtered_count": news_result.get("filtered", 0),
            "replaced": news_result.get("replaced", False),
            "replacement": news_result.get("replacement", ""),
            "detail": news_result.get("detail", {}),
        },
        "signal_strength": signal_strength,
        "market_state": market_state,
        "checklist": checklist,
    }

    with open(VERIFY_PATH, 'w', encoding='utf-8') as f:
        json.dump(verify_result, f, ensure_ascii=False, indent=2)

    print(f"\n  验证文件: {VERIFY_PATH}")
    return verify_result


# ============================================================
# MAIN
# ============================================================
if __name__ == "__main__":
    print(f"\n{'#' * 60}")
    print(f"#  统一推荐入口 v2.1")
    print(f"#  时间: {dt.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{'#' * 60}\n")

    # Step 1
    ok1, msg1 = run_screener()
    if not ok1:
        with open(VERIFY_PATH, 'w', encoding='utf-8') as f:
            json.dump({
                "passed": False,
                "conclusion": "脚本异常 - " + msg1,
                "timestamp": dt.datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            }, f, ensure_ascii=False, indent=2)
        print(f"\n[最终结论] 不推荐 - {msg1}")
        sys.exit(1)

    # Step 2
    ok2, data, health = verify_and_healthcheck()
    if not ok2:
        print(f"\n[Step 2/5] 数据健康问题: {health.get('issues', [])}")
        # 不阻塞：数据问题需人工判断，但继续流程

    # Step 3
    ok3, data, news_result = run_news_filter(data)
    # 非阻塞

    # Step 3.5
    market_state = check_market_state()

    # Step 4
    if data:
        signal_strength = assess_signal_strength(data)
    else:
        signal_strength = {"level": "N/A", "action": "N/A", "score": 0}

    # Step 5
    final = write_final_output(ok1 and ok2, data, health, news_result, signal_strength, market_state)

    print(f"\n{'#' * 60}")
    print(f"#  最终结论: {final['conclusion']}")
    print(f"#  信号强度: {signal_strength.get('level', '?')}")
    print(f"{'#' * 60}")
    print(f"\n===== 统一入口完成 =====")
    # 更新选股看板数据
    print(f"# 正在更新选股看板...")
    try:
        subprocess.run([sys.executable, os.path.join(WORKSPACE, 'update_dashboard.py')],
                       capture_output=True, text=True, timeout=10,
                       env={**os.environ, 'PYTHONIOENCODING': 'utf-8'})
        print(f"# 看板数据已更新")
    except Exception as e:
        print(f"# [WARN] 看板更新失败: {e}")
    sys.exit(0)
