import sys, os
sys.path.insert(0, os.getcwd())
import akshare as ak
df = ak.stock_sector_spot(indicator='新浪行业')
print('板块数:', len(df), ' 覆盖股票数(合计):', int(df['公司家数'].sum()))
print()
names = list(zip(df['板块'], df['公司家数'], df['label']))
for i in range(0, len(names), 3):
    print('   ' + '   '.join('%-12s(%3d)' % (n, c) for n, c, l in names[i:i+3]))