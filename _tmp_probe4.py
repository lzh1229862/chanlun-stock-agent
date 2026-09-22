import sys, os, time
sys.path.insert(0, os.getcwd())
import akshare as ak
def probe(name, fn):
    t = time.time()
    try:
        df = fn()
        print('OK   %-30s %5.2fs shape=%s' % (name, time.time()-t, df.shape))
        print('     列:', list(df.columns))
        print('     前 3:'); print(df.head(3).to_string())
        return df
    except Exception as e:
        print('FAIL %-30s %5.2fs %s: %s' % (name, time.time()-t, type(e).__name__, str(e)[:100]))
    print()
probe('stock_industry_clf_hist_sw', lambda: ak.stock_industry_clf_hist_sw())