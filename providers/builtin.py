"""内置数据源：腾讯日线（不复权）+ 新浪复权因子 / 指数 + akshare 名录。

这是项目的默认源，也是 `verify_datasource.py` 拿来当参照的那个。
实现全部复用现有代码（`storage_kline._tx_raw` 等），这一层只做转发。
"""
import datasource as ds


class BuiltinSource(ds.DataSource):
    name = "builtin"
    description = "腾讯日线（不复权）+ 新浪复权因子 / 指数 + akshare 名录"
    provides = ("kline", "factor", "index_kline", "universe")

    def kline(self, code, start, end):
        import storage_kline as sk
        return sk._tx_raw(code, start, end)

    def factor(self, code, start, end):
        # 新浪的因子一股给全历史，start 用不上
        import storage_kline as sk
        return sk._sina_factor(code, end)

    def index_kline(self, code):
        import storage_index as si
        return si._sina_index(code)

    def universe(self):
        import market_scan as ms
        return ms._ak_universe()


ds.register(BuiltinSource())