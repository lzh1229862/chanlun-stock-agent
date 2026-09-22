import sys, os, time
sys.path.insert(0, os.getcwd())
import akshare as ak
print('所有 *_ths 函数:')
print('  ', [x for x in dir(ak) if x.endswith('_ths')])
print()
print('所有含 cons 的函数:')
print('  ', [x for x in dir(ak) if 'cons' in x])
print()
import fundamentals as fd
t = time.time()
try:
    p = fd.fetch_profile_cninfo('600519')
    print('巨潮 profile 600519: %.2fs' % (time.time()-t), {k: str(v)[:22] for k, v in p.items()})
except Exception as e:
    print('FAIL', type(e).__name__, str(e)[:100])
t = time.time()
try:
    p2 = fd.fetch_profile_cninfo('300476')
    print('巨潮 profile 300476: %.2fs 行业=%s' % (time.time()-t, p2.get('industry')))
except Exception as e:
    print('FAIL', type(e).__name__, str(e)[:100])