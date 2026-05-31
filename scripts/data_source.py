"""
data_source.py — 第三方数据源抽象层
提供统一的数据源接口，支持多数据源注册与切换
当前支持: akshare (A股), akshare_hk (港股), tushare (可选)
"""

import logging
from abc import ABC, abstractmethod
from datetime import date, timedelta
from typing import Optional

logger = logging.getLogger("invest_system.data_source")


class DataSource(ABC):
    """数据源抽象基类"""

    def __init__(self, name: str):
        self.name = name
        self._available = None

    @abstractmethod
    def check_availability(self) -> bool:
        """检查数据源是否可用"""
        ...

    @abstractmethod
    def fetch_daily(self, ts_code: str, start_date: str = None, end_date: str = None) -> list[dict]:
        """获取日线行情"""
        ...

    @abstractmethod
    def fetch_financial(self, ts_code: str) -> dict:
        """获取财务指标"""
        ...

    @abstractmethod
    def fetch_realtime(self, ts_code: str) -> dict:
        """获取实时行情"""
        ...

    def is_available(self) -> bool:
        """缓存可用性检查"""
        if self._available is None:
            self._available = self.check_availability()
        return self._available


class AkshareSource(DataSource):
    """akshare A股数据源"""

    def __init__(self):
        super().__init__("akshare")

    def check_availability(self) -> bool:
        try:
            import akshare
            return True
        except ImportError:
            return False

    def fetch_daily(self, ts_code: str, start_date: str = None, end_date: str = None) -> list[dict]:
        if end_date is None:
            end_date = date.today().strftime("%Y%m%d")
        if start_date is None:
            start_date = (date.today() - timedelta(days=60)).strftime("%Y%m%d")

        try:
            import akshare as ak
            code = ts_code.split(".")[0]
            df = ak.stock_zh_a_hist(
                symbol=code, period="daily",
                start_date=start_date, end_date=end_date, adjust="qfq",
            )
            if df is None or df.empty:
                return []
            df = df.rename(columns={
                "日期": "trade_date", "开盘": "open", "最高": "high",
                "最低": "low", "收盘": "close", "成交量": "volume",
                "涨跌幅": "change_pct",
            })
            df["ts_code"] = ts_code
            df["trade_date"] = df["trade_date"].astype(str)
            return df.to_dict("records")
        except Exception as e:
            logger.warning(f"akshare 日线采集失败 {ts_code}: {e}")
            return []

    def fetch_financial(self, ts_code: str) -> dict:
        try:
            import akshare as ak
            code = ts_code.split(".")[0]
            df = ak.stock_financial_abstract_ths(symbol=code)
            if df is None or df.empty:
                return {}
            latest = df.iloc[-1].to_dict()
            return {
                "ts_code": ts_code,
                "eps": float(latest.get("基本每股收益", 0) or 0),
                "bps": float(latest.get("每股净资产", 0) or 0),
                "roe": float(latest.get("净资产收益率", 0) or 0),
                "profit_growth": float(latest.get("净利润同比增长率", 0) or 0),
            }
        except Exception as e:
            logger.warning(f"akshare 财务采集失败 {ts_code}: {e}")
            return {}

    def fetch_realtime(self, ts_code: str) -> dict:
        try:
            import akshare as ak
            code = ts_code.split(".")[0]
            df = ak.stock_zh_a_spot_em()
            if df is None or df.empty:
                return {}
            row = df[df["代码"] == code]
            if row.empty:
                return {}
            r = row.iloc[0]
            return {
                "ts_code": ts_code,
                "name": str(r.get("名称", "")),
                "price": float(r.get("最新价", 0) or 0),
                "change_pct": float(r.get("涨跌幅", 0) or 0),
                "volume": int(r.get("成交量", 0) or 0),
            }
        except Exception as e:
            logger.warning(f"akshare 实时行情失败 {ts_code}: {e}")
            return {}


class AkshareHKSource(DataSource):
    """akshare 港股数据源"""

    def __init__(self):
        super().__init__("akshare_hk")

    def check_availability(self) -> bool:
        try:
            import akshare
            return True
        except ImportError:
            return False

    def fetch_daily(self, ts_code: str, start_date: str = None, end_date: str = None) -> list[dict]:
        from hk_fetcher import fetch_hk_daily
        return fetch_hk_daily(ts_code, start_date, end_date)

    def fetch_financial(self, ts_code: str) -> dict:
        from hk_fetcher import fetch_hk_financial
        return fetch_hk_financial(ts_code)

    def fetch_realtime(self, ts_code: str) -> dict:
        from hk_fetcher import fetch_hk_realtime_quote
        return fetch_hk_realtime_quote(ts_code)


class DataSourceRegistry:
    """数据源注册中心"""

    _instance = None
    _sources: dict[str, DataSource] = {}

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def register(self, source: DataSource):
        """注册数据源"""
        self._sources[source.name] = source
        logger.info(f"数据源已注册: {source.name}")

    def get(self, name: str) -> Optional[DataSource]:
        """获取指定数据源"""
        return self._sources.get(name)

    def get_available(self) -> list[DataSource]:
        """获取所有可用数据源"""
        return [s for s in self._sources.values() if s.is_available()]

    def get_for_market(self, ts_code: str) -> Optional[DataSource]:
        """根据代码自动选择数据源"""
        if ts_code.endswith(".HK"):
            return self.get("akshare_hk")
        return self.get("akshare")

    def list_sources(self) -> list[dict]:
        """列出所有注册数据源及状态"""
        return [
            {"name": s.name, "available": s.is_available()}
            for s in self._sources.values()
        ]


def get_registry() -> DataSourceRegistry:
    """获取数据源注册中心单例"""
    return DataSourceRegistry()


def init_default_sources():
    """
    初始化默认数据源
    注册 akshare (A股) 和 akshare_hk (港股)
    """
    registry = get_registry()
    registry.register(AkshareSource())
    registry.register(AkshareHKSource())
    available = registry.list_sources()
    logger.info(f"数据源初始化完成: {available}")


def fetch_market_data(ts_code: str, data_type: str = "daily", **kwargs) -> dict:
    """
    统一数据获取接口

    Args:
        ts_code: 股票代码 (如 000001.XSHE, 00700.HK)
        data_type: 数据类型 (daily/financial/realtime)
        **kwargs: 额外参数

    Returns:
        {"source": str, "data": list/dict, "error": Optional[str]}
    """
    registry = get_registry()
    source = registry.get_for_market(ts_code)

    if source is None:
        return {"source": None, "data": None, "error": f"无可用数据源: {ts_code}"}

    if not source.is_available():
        return {"source": source.name, "data": None, "error": f"数据源不可用: {source.name}"}

    try:
        if data_type == "daily":
            data = source.fetch_daily(ts_code, **kwargs)
        elif data_type == "financial":
            data = source.fetch_financial(ts_code)
        elif data_type == "realtime":
            data = source.fetch_realtime(ts_code)
        else:
            return {"source": source.name, "data": None, "error": f"未知数据类型: {data_type}"}

        return {"source": source.name, "data": data, "error": None}
    except Exception as e:
        return {"source": source.name, "data": None, "error": str(e)}


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    init_default_sources()

    registry = get_registry()
    print(f"已注册数据源: {registry.list_sources()}")

    # 测试 A 股
    result = fetch_market_data("000001.XSHE", "realtime")
    print(f"A股: {result['source']} -> {result.get('data', {}).get('price')}")

    # 测试港股
    result2 = fetch_market_data("00700", "realtime")
    print(f"港股: {result2['source']} -> {result2.get('data', {}).get('price')}")