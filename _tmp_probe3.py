import sys, os
sys.path.insert(0, os.getcwd())
import akshare as ak
ns = [x for x in dir(ak) if 'industry' in x or ('sector' in x) or x.startswith('sw_')]
print('候选函数:'); [print('  ', x) for x in ns]