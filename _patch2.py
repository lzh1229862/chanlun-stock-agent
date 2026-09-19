from pathlib import Path
import ast

NL = chr(10)
Q = chr(34)

# ---------- A) signal_filter 自测增加第 6 节 ----------
p = Path("signal_filter.py")
s = p.read_text(encoding="utf-8")
a = "    print()" + NL + "    print(f" + Q + "==> 自测 {sum(ok)}/{len(ok)} 通过" + Q
assert s.count(a) == 1, "selftest anchor " + str(s.count(a))

lines = []
lines.append("    print(" + Q + "--- 6) 量能过滤（F3.4）---" + Q + ")")
lines.append("    def mk(vols, amts):")
lines.append("        import pandas as _pd")
lines.append("        return _pd.DataFrame({" + Q + "date" + Q + ": _pd.date_range(" + Q + "2026-01-01" + Q + ", periods=len(vols), freq=" + Q + "D" + Q + "),")
lines.append("                              " + Q + "open" + Q + ": [100.0]*len(vols), " + Q + "high" + Q + ": [100.0]*len(vols),")
lines.append("                              " + Q + "low" + Q + ": [100.0]*len(vols), " + Q + "close" + Q + ": [100.0]*len(vols),")
lines.append("                              " + Q + "qfq_factor" + Q + ": [1.0]*len(vols),")
lines.append("                              " + Q + "volume" + Q + ": vols, " + Q + "amount" + Q + ": amts})")
lines.append("    one_buy = [{" + Q + "date" + Q + ": " + Q + "2026-01-01" + Q + ", " + Q + "type" + Q + ": " + Q + "第一类买点" + Q + "}]")
lines.append("    v_on = {**base_ctx, " + Q + "volume" + Q + ": {" + Q + "enable" + Q + ": True, " + Q + "exclude_suspended" + Q + ": True,")
lines.append("                                    " + Q + "min_amount" + Q + ": 1.0e7, " + Q + "max_volume_ratio" + Q + ": None}}")
lines.append("    v_off = {**base_ctx, " + Q + "volume" + Q + ": {" + Q + "enable" + Q + ": False, " + Q + "min_amount" + Q + ": 1.0e7}}")
lines.append("    vb = mk([1000.0, 20000.0], [1.0e6, 5.0e6])")
lines.append("    rv = filter_signals(" + Q + "600519" + Q + ", one_buy, vb, v_on)")
lines.append("    check(" + Q + "成交额不足 -> 不可交易" + Q + ", rv[0][" + Q + "is_tradable" + Q + "], 0)")
lines.append("    check(" + Q + "成交额不足 code=illiquid" + Q + ", rv[0][" + Q + "filter_codes" + Q + "], " + Q + "illiquid" + Q + ")")
lines.append("    rv2 = filter_signals(" + Q + "600519" + Q + ", one_buy, vb, v_off)")
lines.append("    check(" + Q + "量能过滤关闭 -> 可交易" + Q + ", rv2[0][" + Q + "is_tradable" + Q + "], 1)")
lines.append("    check(" + Q + "关闭时不写 volume detail" + Q + ", " + Q + "成交量" + Q + " in rv2[0][" + Q + "detail" + Q + "], False)")
lines.append("    vb2 = mk([0.0, 100.0], [0.0, 1.0e8])")
lines.append("    check(" + Q + "停牌 -> code=susp" + Q + ", filter_signals(" + Q + "600519" + Q + ", one_buy, vb2, v_on)[0][" + Q + "filter_codes" + Q + "], " + Q + "susp" + Q + ")")
lines.append("    vb3 = mk([100.0]*20 + [5000.0], [1.0e8]*21)")
lines.append("    v3 = {**base_ctx, " + Q + "volume" + Q + ": {" + Q + "enable" + Q + ": True, " + Q + "max_volume_ratio" + Q + ": 10.0}}")
lines.append("    late_buy = [{" + Q + "date" + Q + ": " + Q + "2026-01-21" + Q + ", " + Q + "type" + Q + ": " + Q + "第一类买点" + Q + "}]")
lines.append("    check(" + Q + "放量异常 -> code=volspike" + Q + ", filter_signals(" + Q + "600519" + Q + ", late_buy, vb3, v3)[0][" + Q + "filter_codes" + Q + "], " + Q + "volspike" + Q + ")")
lines.append("")

sec = NL.join(lines)
s = s.replace(a, sec + a)
p.write_text(s, encoding="utf-8", newline=NL)
ast.parse(s)
print("A) signal_filter selftest 已扩充")

# ---------- B) report_builder 的 FILTER_LEGEND ----------
p2 = Path("report_builder.py")
t = p2.read_text(encoding="utf-8")
b = ("    " + Q + "limitup" + Q + ": " + Q + "买点当日涨停，买不进" + Q + ", "
     + Q + "limitdown" + Q + ": " + Q + "卖点当日跌停，卖不出" + Q + ",")
assert t.count(b) == 1, "legend anchor " + str(t.count(b))
b2 = (b + NL
      + "    " + Q + "susp" + Q + ": " + Q + "信号日停牌（成交量为 0）" + Q + "," + NL
      + "    " + Q + "illiquid" + Q + ": " + Q + "成交额低于下限，流动性不足" + Q + "," + NL
      + "    " + Q + "volspike" + Q + ": " + Q + "成交量异常放大" + Q + ",")
t = t.replace(b, b2)
p2.write_text(t, encoding="utf-8", newline=NL)
ast.parse(t)
print("B) report_builder FILTER_LEGEND 已扩充")

# ---------- C) backfill_t4 的 RULE_LEGEND ----------
p3 = Path("backfill_t4.py")
u = p3.read_text(encoding="utf-8")
c = "    (" + Q + "limitdown" + Q + ", " + Q + "卖点当日跌停，卖不出" + Q + "),"
assert u.count(c) == 1, "rule legend anchor " + str(u.count(c))
c2 = (c + NL
      + "    (" + Q + "susp" + Q + ", " + Q + "信号日停牌（成交量为 0）" + Q + ")," + NL
      + "    (" + Q + "illiquid" + Q + ", " + Q + "成交额低于下限，流动性不足" + Q + ")," + NL
      + "    (" + Q + "volspike" + Q + ", " + Q + "成交量异常放大" + Q + "),")
u = u.replace(c, c2)
p3.write_text(u, encoding="utf-8", newline=NL)
ast.parse(u)
print("C) backfill_t4 RULE_LEGEND 已扩充")
