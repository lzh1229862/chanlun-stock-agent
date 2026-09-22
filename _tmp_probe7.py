import sys, os, time
sys.path.insert(0, os.getcwd())
import akshare as ak, fundamentals as fd
print('fundamentals 里的 fetch_*:', [x for x in dir(fd) if x.startswith('fetch_')])
print()
def probe(name, fn):
    t = time.time()
    try:
        df = fn()
        print('OK   %-32s %5.2fs shape=%s 列=%s' % (name, time.time()-t, df.shape, list(df.columns)[:6]))
        print('     前2:', df.head(2).to_dict('records'))
    except Exception as e:
        print('FAIL %-32s %5.2fs %s: %s' % (name, time.time()-t, type(e).__name__, str(e)[:85]))
    print()
probe('sw_index_third_info', lambda: ak.sw_index_third_info())
probe('sw_index_third_cons 801001', lambda: ak.sw_index_third_cons(symbol='801001'))
probe('stock_board_industry_cons_em 半导体', lambda: ak.stock_board_industry_cons_em(symbol='半导体'))