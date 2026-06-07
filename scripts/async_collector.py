"""
async_collector.py — 异步 I/O 数据采集模块
基于 asyncio + aiohttp 实现并发的行情/新闻/财务数据采集
大幅降低多标的采集的总耗时
"""

import asyncio
import logging
import time
from datetime import date, timedelta
from typing import Callable

import aiohttp

logger = logging.getLogger("invest_system.async_collector")


async def fetch_url(session: aiohttp.ClientSession, url: str, timeout: int = 30) -> dict:
    """
    异步获取 URL 内容

    Args:
        session: aiohttp 会话
        url: 目标 URL
        timeout: 超时秒数

    Returns:
        {"status": int, "data": bytes/str, "error": Optional[str]}
    """
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=timeout)) as resp:
            text = await resp.text()
            return {"status": resp.status, "data": text, "error": None}
    except asyncio.TimeoutError:
        return {"status": 0, "data": None, "error": "超时"}
    except Exception as e:
        return {"status": 0, "data": None, "error": str(e)}


async def async_fetch_quotes(ts_codes: list[str]) -> dict[str, dict]:
    """
    异步批量获取股票实时行情

    Args:
        ts_codes: 股票代码列表

    Returns:
        {ts_code: quote_dict} 映射
    """
    try:
        import akshare as ak
        loop = asyncio.get_event_loop()
        df = await loop.run_in_executor(None, ak.stock_zh_a_spot_em)
        if df is None or df.empty:
            return {}

        result = {}
        for ts_code in ts_codes:
            code = ts_code.split(".")[0]
            row = df[df["代码"] == code]
            if not row.empty:
                r = row.iloc[0]
                result[ts_code] = {
                    "name": str(r.get("名称", "")),
                    "price": float(r.get("最新价", 0) or 0),
                    "change_pct": float(r.get("涨跌幅", 0) or 0),
                }
        return result
    except ImportError:
        logger.warning("akshare 未安装")
        return {}
    except Exception as e:
        logger.warning(f"异步行情采集失败: {e}")
        return {}


async def async_collect_all(
    ts_codes: list[str],
    collectors: list[Callable] = None,
) -> dict:
    """
    并发执行多个采集任务

    Args:
        ts_codes: 股票代码列表
        collectors: 采集函数列表

    Returns:
        {"results": [...], "elapsed_ms": float, "success_count": int}
    """
    if collectors is None:
        collectors = [async_fetch_quotes]

    start = time.time()
    tasks = [asyncio.create_task(collector(ts_codes)) for collector in collectors]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    elapsed = (time.time() - start) * 1000
    success = sum(1 for r in results if not isinstance(r, Exception))

    return {
        "results": results,
        "elapsed_ms": round(elapsed, 1),
        "success_count": success,
        "total_tasks": len(collectors),
    }


def run_async_collection(ts_codes: list[str]) -> dict:
    """
    同步包装器：运行异步采集

    Args:
        ts_codes: 股票代码列表

    Returns:
        采集结果
    """
    return asyncio.run(async_collect_all(ts_codes))


class AsyncBatchCollector:
    """
    异步批量数据采集器
    支持并发采集行情、财务、新闻等多类数据

    Args:
        max_concurrency: 最大并发数
    """

    def __init__(self, max_concurrency: int = 5):
        self.max_concurrency = max_concurrency
        self._semaphore = asyncio.Semaphore(max_concurrency)

    async def _fetch_one_quote(self, ts_code: str) -> dict:
        """采集单只股票行情"""
        async with self._semaphore:
            try:
                import akshare as ak
                loop = asyncio.get_event_loop()
                code = ts_code.split(".")[0]
                df = await loop.run_in_executor(None, lambda: ak.stock_zh_a_hist(
                    symbol=code, period="daily",
                    start_date=(date.today() - timedelta(days=5)).strftime("%Y%m%d"),
                    end_date=date.today().strftime("%Y%m%d"),
                    adjust="qfq",
                ))
                if df is not None and not df.empty:
                    latest = df.iloc[-1]
                    return {
                        "ts_code": ts_code,
                        "close": float(latest.get("收盘", 0) or 0),
                        "change_pct": float(latest.get("涨跌幅", 0) or 0),
                        "volume": int(latest.get("成交量", 0) or 0),
                    }
                return {"ts_code": ts_code, "close": 0}
            except Exception as e:
                return {"ts_code": ts_code, "error": str(e)}

    async def collect_all_quotes(self, ts_codes: list[str]) -> dict[str, dict]:
        """
        并发采集所有股票行情

        Args:
            ts_codes: 股票代码列表

        Returns:
            {ts_code: quote_dict}
        """
        tasks = [self._fetch_one_quote(code) for code in ts_codes]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        return {
            r["ts_code"]: r
            for r in results
            if isinstance(r, dict) and "ts_code" in r
        }

    def run_sync(self, ts_codes: list[str]) -> dict[str, dict]:
        """
        同步运行异步采集

        Args:
            ts_codes: 股票代码列表

        Returns:
            {ts_code: quote_dict}
        """
        return asyncio.run(self.collect_all_quotes(ts_codes))


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)

    print("=== 异步采集测试 ===")
    test_codes = ["000001.XSHE", "000858.XSHE"]
    collector = AsyncBatchCollector(max_concurrency=3)
    results = collector.run_sync(test_codes)
    for code, data in results.items():
        print(f"  {code}: close={data.get('close')}, change={data.get('change_pct')}%")