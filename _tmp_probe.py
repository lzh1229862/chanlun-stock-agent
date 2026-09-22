import sys, os, traceback
sys.path.insert(0, os.getcwd())
import akshare as ak
import sqlite3
c = sqlite3.connect('data/chan_agent.db')
n, k = c.execute('SELECT COUNT(*), COUNT(industry) FROM profiles').fetchone() if True else (0,0)
print('profiles 表: %d 行, 其中有 industry 的 %d 行' % (n, k))
print()
def probe(name, fn):
    try:
        df = fn()
        print('OK   %-38s shape=%s' % (name, getattr(df, 'shape', '?')))
        print('     列:', list(df.columns)[:9])
        print('     前 2 行:', df.head(2).to_dict('records')[:2])
    except Exception as e:
        print('FAIL %-38s %s: %s' % (name, type(e).__name__, str(e)[:80]))
    print()
probe('新浪行业板块 stock_sector_spot', lambda: ak.stock_sector_spot(indicator='新浪行业'))
probe('同花顺行业列表 stock_board_industry_name_ths', lambda: ak.stock_board_industry_name_ths())