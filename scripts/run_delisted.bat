@echo off
REM 退市股取数（T24）。断点续跑：已有 parquet 的自动跳过。
REM 252 只 x 约 11 秒 ~= 46 分钟。需要网络（腾讯行情）。
cd /d "%~dp0.."
:loop
python delisted_pool.py --limit 40 > logs\delisted.log 2>&1
for %%f in (data\delisted\*.parquet) do set /a n+=1
python -c "import delisted_pool as d; pool=d.load_pool(); import pathlib; done=len(list(pathlib.Path('data/delisted').glob('*.parquet'))); print('done %d / %d' % (done, len(pool)))" >> logs\delisted.log 2>&1
if exist data\_delisted_done goto :eof
goto :loop
