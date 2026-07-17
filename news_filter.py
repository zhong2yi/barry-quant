"""
新闻/公告利空过滤脚本 - news_filter.py (新浪源版)
数据源: 新浪财经个股新闻页 (vip.stock.finance.sina.com.cn)，沙箱内可用
（原 NEODATA 腾讯 copilot API 在沙箱返回 API_ERROR，已弃用）

用法:
  python news_filter.py --codes sh603956,sz002415 --days 7

输出: news_filter_result.json
  {
    "filtered_out": [{"code": "sh603956", "name": "威派格", "reasons": ["ST"]}],
    "passed": [{"code": "sh600919", "name": "江苏银行", "risk_level": "LOW"}],
    "scan_time": "2026-07-17 09:05:00"
  }
"""
import json, os, sys, re, datetime as dt, requests

sys.stdout.reconfigure(encoding='utf-8', line_buffering=True)

WORKSPACE = os.path.dirname(os.path.abspath(__file__))
RESULT_PATH = os.path.join(WORKSPACE, "news_filter_result.json")
PYTHON = sys.executable
T_HEADERS = {'User-Agent': 'Mozilla/5.0', 'Referer': 'https://finance.sina.com.cn'}

# === 利空关键词体系（三层分级）===

# 严重利空（标题/正文命中且关联本标的 → RED）
RED_KEYWORDS_SEVERE = [
    "股权被冻结", "银行账户被冻结", "立案调查", "退市",
    "ST", "*ST", "暴雷", "信披违规", "隐瞒不披露",
    "暂停上市", "终止上市", "破产", "重大诉讼", "违规担保",
]

# 中度利空（需确认是针对本标的，不是行业通用词）
RED_KEYWORDS_MODERATE = [
    "诉讼", "冻结", "索赔", "仲裁", "合同纠纷",
    "被指", "违规", "处罚", "亏损", "业绩预亏",
]

# 行业通用词（不算利空，比如银行的"监管"是常态）
INDUSTRY_NEUTRAL_WORDS = [
    "监管", "减持", "警示", "问询函", "澄清",
]

# 黄色预警关键词（命中标记为YELLOW，不自动排除但提醒）
YELLOW_KEYWORDS = [
    "定增", "增发", "稀释", "偏离基本面", "风险提示",
    "非理性炒作", "交易异常波动", "高管减持", "业绩预亏", "大幅下滑",
]

# 排除关键词（这些词出现在利空新闻的"澄清"中，不算利空）
WHITELIST_PHRASES = [
    "不存在应披露未披露", "澄清说明", "经营正常", "无重大影响",
    "经营稳定", "不构成重大影响",
]


def fetch_sina_news(code: str) -> str:
    """抓取新浪财经个股新闻页 HTML（沙箱可用）"""
    sym = code[2:] if len(code) > 6 else code  # sh603956 -> 603956
    u = f'https://vip.stock.finance.sina.com.cn/corp/go.php/vCB_AllNewsStock/symbol/{sym}/p1.html'
    try:
        r = requests.get(u, headers=T_HEADERS, timeout=8)
        if r.status_code == 200 and len(r.text) > 200:
            return r.text
    except Exception:
        pass
    return ""


def analyze_risk(code: str, name: str, html: str) -> dict:
    """在新浪新闻 HTML 中搜索利空关键词并定级（仅计入关联本标的的命中）"""
    if not html:
        return {"code": code, "name": name, "risk_level": "UNKNOWN",
                "reasons": ["新浪新闻页获取失败"], "red_hits": [], "yellow_hits": []}

    short_name = name[:2] if name else code
    cx = code[-6:] if len(code) > 6 else code
    raw = code[2:] if len(code) > 6 else code

    red_hits = []
    yellow_hits = []

    def _related(kw):
        """关键词附近是否出现公司名/代码，确认是针对本标的（排除行业泛化命中）"""
        idx = html.find(kw)
        if idx < 0:
            return False
        ctx = html[max(0, idx - 80): idx + 80]
        return short_name in ctx or cx in ctx or raw in ctx

    # 严重关键词（关联本标的才计为RED）
    for kw in RED_KEYWORDS_SEVERE:
        if kw in html and _related(kw) and kw not in red_hits:
            red_hits.append(kw)

    # 中度关键词（关联本标的才计为RED）
    for kw in RED_KEYWORDS_MODERATE:
        if kw in html and _related(kw) and kw not in red_hits:
            red_hits.append(kw)

    # 黄色预警关键词（仅关联本标的的才计）
    for kw in YELLOW_KEYWORDS:
        if kw in html and _related(kw) and kw not in yellow_hits:
            yellow_hits.append(kw)

    # 白名单短语（澄清公告）：关联命中且含白名单则降级忽略
    for ph in WHITELIST_PHRASES:
        if ph in html:
            red_hits = [r for r in red_hits if not _related(r)]
            yellow_hits = [y for y in yellow_hits if not _related(y)]

    red_hits = list(dict.fromkeys(red_hits))
    yellow_hits = list(dict.fromkeys(yellow_hits))

    if red_hits:
        risk_level = "RED"
    elif yellow_hits:
        risk_level = "YELLOW"
    else:
        risk_level = "GREEN"

    reasons = []
    if red_hits:
        reasons.append(f"[RED] {', '.join(red_hits)}")
    if yellow_hits:
        reasons.append(f"[YELLOW] {', '.join(yellow_hits)}")

    return {
        "code": code, "name": name, "risk_level": risk_level,
        "reasons": reasons, "red_hits": red_hits, "yellow_hits": yellow_hits,
    }


def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--codes", default="", help="逗号分隔的股票代码")
    parser.add_argument("--days", type=int, default=7, help="查询最近N天新闻(保留兼容)")
    parser.add_argument("--json-input", default="", help="从latest_recommendation.json读取候选")
    args = parser.parse_args()

    # 从JSON读取候选（优先）
    candidates = []
    if args.json_input or os.path.exists(os.path.join(WORKSPACE, "latest_recommendation.json")):
        json_path = args.json_input or os.path.join(WORKSPACE, "latest_recommendation.json")
        try:
            with open(json_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            if data.get("main"):
                candidates.append(data["main"])
            if data.get("main_backup"):
                candidates.append(data["main_backup"])
            for item in data.get("all_shrink", []):
                if item["code"] not in [c["code"] for c in candidates]:
                    candidates.append(item)
        except Exception:
            pass

    # 命令行传入的代码（补充）
    if args.codes:
        for code in args.codes.split(","):
            code = code.strip()
            if code and code not in [c["code"] for c in candidates]:
                candidates.append({"code": code, "name": code})

    if not candidates:
        print("[NEWS FILTER] No candidates to check")
        return

    print(f"[NEWS FILTER] 新浪源检查 {len(candidates)} 只候选...")

    filtered_out = []
    passed = []

    for cand in candidates:
        code = cand["code"]
        name = cand.get("name", code)
        html = fetch_sina_news(code)
        risk = analyze_risk(code, name, html)
        print(f"  {code} {name}: {risk['risk_level']} | {risk['reasons']}")

        if risk["risk_level"] == "RED":
            filtered_out.append(risk)
        else:
            passed.append(risk)

    # 写结果
    result = {
        "filtered_out": filtered_out,
        "passed": passed,
        "scan_time": dt.datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        "total_checked": len(candidates),
        "total_red": len(filtered_out),
        "total_passed": len(passed),
        "source": "sina_news",
    }

    with open(RESULT_PATH, 'w', encoding='utf-8') as f:
        json.dump(result, f, ensure_ascii=False, indent=2)

    print(f"\n[NEWS FILTER] 结果: {len(filtered_out)}只RED过滤, {len(passed)}只通过")
    print(f"[NEWS FILTER] 已保存: {RESULT_PATH}")


if __name__ == "__main__":
    main()
