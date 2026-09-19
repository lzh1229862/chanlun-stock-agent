from pathlib import Path
import ast

NL = chr(10)
Q = chr(34)
p = Path("main.py")
s = p.read_text(encoding="utf-8")

o1 = '    ap.add_argument("--trading-day-only", action="store_true",'
assert s.count(o1) == 1
n1 = ('    ap.add_argument("--analyze", default=None, metavar="CODE",'
      + NL + '                    help="只分析单只股票（调 analyzer.analyze_stock），不跑批量流程")' + NL + o1)
s = s.replace(o1, n1)

o2 = "    codes = parse_stocks(a.stocks)"
assert s.count(o2) == 1
n2 = NL.join([
    "    if a.analyze:",
    "        from analyzer import analyze_stock",
    "        r = analyze_stock(a.analyze, a.date, use_llm=not a.no_llm)",
    "        if not r.get(" + Q + "ok" + Q + "):",
    '            print(f"分析失败: {r.get('error')}")',
    "            return 1",
    '        print(f"{r['code']} {r['name']}  {r['board']}  涨跌幅限制 ±{r['limit_ratio']:.0%}")',
    '        k = r["kline"]',
    '        print(f"K线 {k['rows']} 行  {k['start']} ~ {k['end']}  收盘 {k['close_raw']}")',
    '        st_ = r["structure"]',
    '        print(f"结构 {st_['n_fx']} 分型 / {st_['n_bi']} 笔 / {st_['n_zs']} 中枢  位置 {st_['position']}")',
    '        print(f"信号 {len(r['signals'])} 条（可交易 {r['tradable_count']} / 主信号 {r['primary_count']}）")',
    '        for x in r["signals"][-5:]:',
    '            print(f"  {x['date']} {x['type']:<12} 确认 {x['confirm_date'] or '待确认'}  '
    '入场 {x['entry_ref_price'] or '-'}  主信号 {x['is_primary']}")',
    '        if r.get("llm") and r["llm"].get("text"):',
    '            print()',
    '            print("AI 总结（token %d，费用 ¥%.6f）:" % (r["llm"]["tokens"], r["llm"]["cost"]))',
    '            print(r["llm"]["text"])',
    "        return 0",
    "",
    o2,
])
s = s.replace(o2, n2)
p.write_text(s, encoding="utf-8", newline=NL)
ast.parse(s)
print("main.py: --analyze 已加")
