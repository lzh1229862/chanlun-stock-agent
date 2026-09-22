import sys, os, time
sys.path.insert(0, os.getcwd())
import akshare as ak
def probe(name, fn):
    t = time.time()
    try:
        df = fn()
        print('OK   %-34s %5.2fs shape=%s 列=%s' % (name, time.time()-t, df.shape, list(df.columns)[:7]))
        print('     前 2:', df.head(2).to_dict('records'))
        return df
    except Exception as e:
        print('FAIL %-34s %5.2fs %s: %s' % (name, time.time()-t, type(e).__name__, str(e)[:90]))
    print()
probe('新浪 玻璃行业 成分', lambda: ak.stock_sector_detail(sector='new_blhy'))
probe('同花顺 半导体 成分', lambda: ak.stock_board_industry_cons_ths(symbol='半导体'))