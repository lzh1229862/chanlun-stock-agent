import sys, os, sqlite3
sys.path.insert(0, os.getcwd())
c = sqlite3.connect('data/chan_agent.db'); c.row_factory = sqlite3.Row
d = '2026-09-21'
rows = [dict(r) for r in c.execute('SELECT * FROM scan_results WHERE scan_date=?', (d,))]
print('命中 %d 条 / %d 只' % (len(rows), len({r['stock_code'] for r in rows})))
def board(code):
    if code.startswith('688') or code.startswith('689'): return '科创板'
    if code.startswith('300') or code.startswith('301'): return '创业板'
    if code.startswith('60'): return '沪主板'
    return '深主板'
from collections import Counter
print()
print('板块分布（按股票数）:', dict(Counter(board(x) for x in {r['stock_code'] for r in rows})))
print('类型 x 打分:')
cc = Counter((r['signal_type'], r['score']) for r in rows)
for t in ('第一类买点','第二类买点','第三类买点'):
    print('  %-6s  score2=%-4d score1=%-4d score0=%-4d' % (t, cc[(t,2)], cc[(t,1)], cc[(t,0)]))
print()
top = sorted([r for r in rows if r['score'] == 2], key=lambda r: -(r['amount_yi'] or 0))
print('score=2（★★）且成交额最大的 25 只  —— 这是本项目唯一经过时间样本外验证的筛选口径:')
print('  %-8s %-8s %-6s %-11s %-12s %-12s %8s %8s' % ('代码','名称','板块','类型','信号日','确认日','成交额亿','距中枢'))
for r in top[:25]:
    print('  %-8s %-8s %-6s %-11s %-12s %-12s %8.2f %8s' % (r['stock_code'], (r['stock_name'] or '')[:6], board(r['stock_code']), r['signal_type'], r['signal_date'], r['confirm_date'], r['amount_yi'] or 0, r['zs_gap_days']))
print()
print('score=2 共 %d 条，成交额中位 %.2f 亿' % (len(top), sorted((r['amount_yi'] or 0) for r in top)[len(top)//2]))