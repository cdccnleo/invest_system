"""
intraday_monitor.py — 盘中异动监控
每5分钟扫描持仓股异动（涨跌幅 > 3% 或成交量突增 > 2倍）
触发时发送告警到 Server酱 + 飞书
"""

import os, csv, time, logging, threading
from datetime import datetime, date
from pathlib import Path
from typing import Optional

import psycopg2

from dotenv import load_dotenv
load_dotenv("/home/aileo/invest_system/.env")

from fetch_quotes import collect_quotes
from notification import send_notification, send_warning_alert, send_error_alert

try:
    from credentials import get_credential
except ImportError:
    get_credential = None  # 降级策略：密码为空

logger = logging.getLogger("invest_system.intraday_monitor")

DB_CONFIG = {
    "host": os.environ.get("DB_HOST", "localhost"),
    "user": os.environ.get("DB_USER", "invest_admin"),
    "database": os.environ.get("DB_NAME", "investpilot"),
    "password": get_credential("DB_PASSWORD") if get_credential else "",
}
POSITIONS_CSV = os.environ.get("POSITIONS_CSV", "/mnt/d/Hold/invest-data/positions.csv")

# ── 异动阈值配置 ─────────────────────────────────────────────────────────
ALERT_THRESHOLDS = {
    "price_change_pct": 3.0,      # 涨跌幅超 3% 触发告警
    "volume_surge_ratio": 2.0,    # 成交量超均量 2 倍触发告警
    "monitor_interval_sec": 300,  # 扫描间隔：5分钟
    "ma_cross_enabled": True,     # 均线金叉/死叉告警
}


# ── 数据加载 ──────────────────────────────────────────────────────────────

def load_positions_codes() -> list[dict]:
    """加载持仓代码列表"""
    positions = []
    if os.path.exists(POSITIONS_CSV):
        with open(POSITIONS_CSV, encoding="utf-8") as f:
            for row in csv.DictReader(f):
                if not row.get("code"):
                    continue
                positions.append({
                    "code": str(row["code"]).zfill(6),
                    "name": row.get("name", ""),
                    "type": row.get("type", "stock"),
                })
    return positions


def load_baseline_volumes() -> dict:
    """
    加载历史均量基线
    从 PostgreSQL 读取近20日平均成交量
    返回: {ts_code: avg_volume_20d}
    """
    conn = psycopg2.connect(**DB_CONFIG)
    cur = conn.cursor()
    baseline = {}
    try:
        cur.execute("""
            SELECT ts_code, AVG(volume)::int as avg_vol
            FROM market.daily_quotes
            WHERE trade_date >= CURRENT_DATE - INTERVAL '30 days'
              AND trade_date < CURRENT_DATE
            GROUP BY ts_code
        """)
        for row in cur.fetchall():
            baseline[row[0]] = row[1]
    except Exception as e:
        logger.warning(f"读取均量基线失败: {e}")
    finally:
        conn.close()
    return baseline


# ── 新浪均线数据获取 ──────────────────────────────────────────────────────

import urllib.request

SINA_KLINE_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Referer": "https://finance.sina.com.cn",
}
SINA_KLINE_TIMEOUT = 8


def _sina_symbol(ts_code: str) -> str:
    """将 ts_code 转为新浪 K 线 API 格式"""
    code = ts_code.split(".")[0]
    if ts_code.endswith(".XSHG"):
        return f"sh{code}"
    else:  # .XSHE
        return f"sz{code}"


def fetch_ma_data(ts_code: str) -> dict | None:
    """
    从新浪日 K 线获取 MA5/MA20 数据（前2天：昨日+今日）
    返回: {
        prev_close, prev_ma5, prev_ma20,
        curr_close, curr_ma5, curr_ma20,
        curr_high, curr_low
    } 或 None（失败时）
    """
    sina_sym = _sina_symbol(ts_code)
    url = (
        f"https://money.finance.sina.com.cn/quotes_service/api/json_v2.php"
        f"/CN_MarketData.getKLineData?symbol={sina_sym}&scale=240&ma=5,20&datalen=2"
    )
    try:
        req = urllib.request.Request(url, headers=SINA_KLINE_HEADERS)
        with urllib.request.urlopen(req, timeout=SINA_KLINE_TIMEOUT) as resp:
            import json
            data = json.loads(resp.read().decode("gbk"))
        if not data or len(data) < 2:
            return None
        prev = data[0]  # 昨日
        curr = data[1]  # 今日
        return {
            "prev_close": float(prev["close"]),
            "prev_ma5":   float(prev.get("ma_price5", 0) or 0),
            "prev_ma20":  float(prev.get("ma_price20", 0) or 0),
            "curr_close": float(curr["close"]),
            "curr_ma5":   float(curr.get("ma_price5", 0) or 0),
            "curr_ma20":  float(curr.get("ma_price20", 0) or 0),
            "curr_high":  float(curr["high"]),
            "curr_low":   float(curr["low"]),
        }
    except Exception as e:
        logger.debug(f"MA数据获取失败 {ts_code}: {e}")
        return None


def detect_ma_cross_for_symbol(ts_code: str, curr_price: float,
                               prev_close: float, prev_ma5: float, prev_ma20: float,
                               curr_ma5: float, curr_ma20: float) -> list[dict]:
    """
    检测均线金叉/死叉，返回告警列表（每个事件一个 dict）
    上穿（金叉）：昨日价格在均线下方，今日价格上穿均线
    跌破（死叉）：昨日价格在均线上方，今日价格跌破均线
    """
    events = []

    # ── MA5 金叉/死叉 ────────────────────────────────────────────────────
    if prev_ma5 > 0 and curr_ma5 > 0:
        # 金叉：价格从 MA5 下方上穿到上方
        if prev_close < prev_ma5 and curr_price >= curr_ma5:
            events.append({
                "alert_type": "MA5_CROSS_UP",
                "reason": f"MA5金叉(现价{curr_price:.2f}>MA5{curr_ma5:.2f})",
            })
        # 死叉：价格从 MA5 上方下穿到下方
        elif prev_close > prev_ma5 and curr_price <= curr_ma5:
            events.append({
                "alert_type": "MA5_CROSS_DOWN",
                "reason": f"MA5死叉(现价{curr_price:.2f}<MA5{curr_ma5:.2f})",
            })

    # ── MA20 金叉/死叉 ───────────────────────────────────────────────────
    if prev_ma20 > 0 and curr_ma20 > 0:
        # 金叉：价格从 MA20 下方上穿到上方
        if prev_close < prev_ma20 and curr_price >= curr_ma20:
            events.append({
                "alert_type": "MA20_CROSS_UP",
                "reason": f"MA20金叉(现价{curr_price:.2f}>MA20{curr_ma20:.2f})",
            })
        # 死叉：价格从 MA20 上方下穿到下方
        elif prev_close > prev_ma20 and curr_price <= curr_ma20:
            events.append({
                "alert_type": "MA20_CROSS_DOWN",
                "reason": f"MA20死叉(现价{curr_price:.2f}<MA20{curr_ma20:.2f})",
            })

    return events


# ── 异动检测 ─────────────────────────────────────────────────────────────

def detect_anomalies(quotes: list[dict], baseline: dict, name_map: dict = None) -> list[dict]:
    """
    检测异动标的
    name_map: {ts_code: name} 可选，从持仓数据传入以补全股票名称
    返回异动列表: [{ts_code, name, change_pct, volume, avg_vol, volume_ratio, alert_type}]
    """
    anomalies = []
    price_threshold = ALERT_THRESHOLDS["price_change_pct"]
    volume_threshold = ALERT_THRESHOLDS["volume_surge_ratio"]

    for q in quotes:
        ts_code = q.get("ts_code", "")
        change_pct = abs(q.get("change_pct", 0))
        volume = q.get("volume", 0)
        avg_vol = baseline.get(ts_code, 0)
        volume_ratio = volume / avg_vol if avg_vol > 0 else 0

        alert_type = None
        reason = ""

        # 涨跌幅异动
        if change_pct >= price_threshold:
            alert_type = "PRICE_ALERT"
            direction = "上涨" if q.get("change_pct", 0) > 0 else "下跌"
            reason = f"{direction}{change_pct:.1f}%"

        # 成交量异动
        if volume_ratio >= volume_threshold and volume > 0:
            alert_type = (alert_type or "VOLUME_ALERT") + "+VOLUME"
            reason += f" | 成交量为均量的{volume_ratio:.1f}倍"

        if alert_type:
            # 优先从 name_map 取名称（持仓表），其次从行情数据，最后用代码本身
            display_name = (name_map.get(ts_code) if name_map else None) \
                or q.get("name") or ts_code.split(".")[0]
            anomalies.append({
                "ts_code": ts_code,
                "name": display_name,
                "change_pct": q.get("change_pct", 0),
                "close": q.get("close", 0),
                "volume": volume,
                "avg_vol": avg_vol,
                "volume_ratio": round(volume_ratio, 1),
                "alert_type": alert_type,
                "reason": reason.strip(),
                "detected_at": datetime.now().strftime("%H:%M"),
            })

    return anomalies


def format_anomaly_message(anomalies: list[dict]) -> str:
    """格式化异动告警消息"""
    if not anomalies:
        return "无异动信号。"

    # 分类：价格/成交量异动 vs 均线交叉
    price_vol_anomalies = [a for a in anomalies if a.get("alert_type") in (
        "PRICE_ALERT", "PRICE_ALERT+VOLUME", "VOLUME_ALERT", "PRICE_ALERT+VOLUME+VOLUME",
        "PRICE_ALERT+VOLUME+VOLUME",  # extra VOLUME from combined
    ) or (a.get("change_pct", 0) != 0 and a.get("alert_type", "") == "")]  # fallback
    # Simpler: split by whether change_pct != 0
    price_vol_anomalies = [a for a in anomalies if a.get("change_pct", 0) != 0 or "VOLUME" in (a.get("alert_type") or "")]
    ma_cross_anomalies  = [a for a in anomalies if "CROSS" in (a.get("alert_type") or "")]

    lines = []

    # 价格/成交量异动
    if price_vol_anomalies:
        for a in price_vol_anomalies:
            alert = a.get("alert_type") or ""
            emoji = "🔴" if abs(a["change_pct"]) >= 5 else "🟡"
            lines.append(
                f"{emoji} {a['name']}({a['ts_code']})\n"
                f"   现价 {a['close']} | {a['reason']}"
            )

    # 均线交叉
    if ma_cross_anomalies:
        for a in ma_cross_anomalies:
            alert_type = a.get("alert_type") or ""
            if "CROSS_UP" in alert_type:
                emoji = "🟢"
                direction = "金叉"
            else:
                emoji = "🔴"
                direction = "死叉"
            lines.append(
                f"{emoji} {a['name']}({a['ts_code']})\n"
                f"   现价 {a['close']} | {a['reason']}"
            )

    header = f"⚠️ 盘中异动告警 | {datetime.now().strftime('%H:%M')}\n"
    return header + "\n\n".join(lines)


# ── 监控循环 ──────────────────────────────────────────────────────────────

class IntradayMonitor:
    """盘中异动监控器"""

    # 冷却状态持久化文件（进程重启后仍保留30分钟冷却）
    _COOLDOWN_FILE = Path("/home/aileo/invest_system/logs/alert_cooldown.json")

    def __init__(self):
        self.positions = load_positions_codes()
        self.baseline = load_baseline_volumes()
        self.last_alert_time: Optional[datetime] = None
        self.alert_cooldown_sec = 1800  # 同一标的30分钟内不重复告警
        self.alerted_stocks: dict = self._load_cooldown()  # {ts_code: last_alert_time_iso}
        self._running = False
        self._thread: Optional[threading.Thread] = None

    # ── 冷却状态持久化（进程间共享）─────────────────────────────────────

    def _load_cooldown(self) -> dict:
        """从文件加载冷却状态，过期条目自动清除"""
        if not self._COOLDOWN_FILE.exists():
            return {}
        try:
            import json
            data = json.loads(self._COOLDOWN_FILE.read_text())
            now = datetime.now()
            valid = {}
            for ts, iso in data.items():
                last = datetime.fromisoformat(iso)
                if (now - last).total_seconds() < self.alert_cooldown_sec:
                    valid[ts] = iso  # 保留未过期的
                # else: 自动丢弃已过期条目
            return valid
        except Exception:
            return {}

    def _parse_last_time(self, val) -> Optional[datetime]:
        """解析冷却记录：支持 datetime 对象或 ISO 字符串"""
        if val is None:
            return None
        if isinstance(val, datetime):
            return val
        try:
            return datetime.fromisoformat(str(val))
        except Exception:
            return None

    def _save_cooldown(self):
        """持久化冷却状态到文件"""
        try:
            self._COOLDOWN_FILE.parent.mkdir(parents=True, exist_ok=True)
            import json
            self._COOLDOWN_FILE.write_text(json.dumps(self.alerted_stocks))
        except Exception as e:
            logger.debug(f"冷却状态保存失败: {e}")

    def build_symbols(self) -> list[str]:
        """构建监控代码列表"""
        symbols = []
        for pos in self.positions:
            code = pos["code"]
            if pos["type"] == "fund":
                continue  # 基金暂时不监控成交量
            if code.startswith("15") or code.startswith("30") or code.startswith("00"):
                symbols.append(f"{code}.XSHE")
            elif code.startswith("6") or code.startswith("5"):
                symbols.append(f"{code}.XSHG")
            else:
                symbols.append(f"{code}.XSHE")
        return symbols

    def scan(self) -> list[dict]:
        """执行一次扫描"""
        symbols = self.build_symbols()
        if not symbols:
            return []

        quotes, _, _ = collect_quotes(symbols)
        if not quotes:
            logger.warning("异动监控：行情获取失败")
            return []

        # 构建 ts_code → name 映射（从持仓表）
        name_map = {}
        for pos in self.positions:
            code = pos["code"].zfill(6)
            if code.startswith("15") or code.startswith("30") or code.startswith("00"):
                ts = f"{code}.XSHE"
            elif code.startswith("6") or code.startswith("5") or code.startswith("4") or code.startswith("8"):
                ts = f"{code}.XSHG"
            else:
                ts = f"{code}.XSHE"
            name_map[ts] = pos.get("name", code)

        anomalies = detect_anomalies(quotes, self.baseline, name_map)

        # ── 均线金叉/死叉检测 ─────────────────────────────────────────────
        if ALERT_THRESHOLDS.get("ma_cross_enabled", True):
            for pos in self.positions:
                if pos["type"] == "fund":
                    continue
                code = pos["code"].zfill(6)
                if code.startswith("15") or code.startswith("30") or code.startswith("00"):
                    ts = f"{code}.XSHE"
                elif code.startswith("6") or code.startswith("5") or code.startswith("4") or code.startswith("8"):
                    ts = f"{code}.XSHG"
                else:
                    ts = f"{code}.XSHE"

                # 找实时行情（当前价格）
                curr_price = None
                for q in quotes:
                    if q.get("ts_code") == ts:
                        curr_price = q.get("close", 0)
                        break
                if not curr_price or curr_price <= 0:
                    continue

                # 获取 MA 数据（前日+今日）
                ma_data = fetch_ma_data(ts)
                if not ma_data:
                    continue

                # 检测均线交叉
                cross_events = detect_ma_cross_for_symbol(
                    ts, curr_price,
                    ma_data["prev_close"], ma_data["prev_ma5"], ma_data["prev_ma20"],
                    ma_data["curr_ma5"], ma_data["curr_ma20"],
                )
                for evt in cross_events:
                    # 避免重复告警（同标的冷却期内跳过）
                    now = datetime.now()
                    last_time = self._parse_last_time(self.alerted_stocks.get(ts))
                    if last_time and (now - last_time).total_seconds() < self.alert_cooldown_sec:
                        continue
                    display_name = name_map.get(ts, ts.split(".")[0])
                    anomalies.append({
                        "ts_code": ts,
                        "name": display_name,
                        "change_pct": 0.0,
                        "close": curr_price,
                        "volume": 0,
                        "avg_vol": 0,
                        "volume_ratio": 0.0,
                        "alert_type": evt["alert_type"],
                        "reason": evt["reason"],
                        "detected_at": datetime.now().strftime("%H:%M"),
                    })

        # 过滤冷却期内标的，并通过时立即写入冷却状态
        now = datetime.now()
        filtered = []
        for a in anomalies:
            ts = a["ts_code"]
            last_time = self._parse_last_time(self.alerted_stocks.get(ts))
            if last_time and (now - last_time).total_seconds() < self.alert_cooldown_sec:
                logger.debug(f"{ts} 在冷却期内，跳过告警")
                continue
            # 记录冷却（从现在起30分钟内同标的不重复告警）
            self.alerted_stocks[ts] = now.isoformat()
            self._save_cooldown()
            filtered.append(a)

        return filtered

    def run_scan_and_alert(self):
        """扫描 + 推送告警"""
        try:
            anomalies = self.scan()
            if not anomalies:
                return

            msg = format_anomaly_message(anomalies)
            logger.warning(f"检测到 {len(anomalies)} 个异动")

            # 推送告警（冷却状态已在 scan() 中记录）
            send_notification("⚠️ 盘中异动告警", msg, level="WARNING")

        except Exception as e:
            logger.error(f"异动扫描异常: {e}")
            send_error_alert("🔴 异动监控异常", str(e))

    def start(self, interval_sec: int = None):
        """启动监控（后台线程）"""
        if self._running:
            logger.warning("监控已在运行")
            return

        interval = interval_sec or ALERT_THRESHOLDS["monitor_interval_sec"]
        self._running = True

        def loop():
            logger.info(f"盘中异动监控启动（每{interval}秒）")
            while self._running:
                # 仅在交易时间扫描
                now = datetime.now()
                hour = now.hour
                is_trading_hour = (hour == 9 and now.minute >= 30) or (10 <= hour < 11) or (hour == 13) or (hour == 14 and now.minute <= 55)

                if is_trading_hour:
                    self.run_scan_and_alert()
                else:
                    logger.debug(f"非交易时间，跳过扫描 ({now.strftime('%H:%M')})")

                time.sleep(interval)

            logger.info("盘中异动监控已停止")

        self._thread = threading.Thread(target=loop, daemon=True)
        self._thread.start()
        logger.info("盘中异动监控线程已启动")

    def stop(self):
        """停止监控"""
        self._running = False
        if self._thread:
            self._thread.join(timeout=5)
        logger.info("盘中异动监控已停止")


# ── 全局单例 ──────────────────────────────────────────────────────────────

_monitor: Optional[IntradayMonitor] = None


def start_monitor():
    global _monitor
    if _monitor is None:
        _monitor = IntradayMonitor()
    _monitor.start()


def stop_monitor():
    global _monitor
    if _monitor:
        _monitor.stop()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)

    print("=== 盘中异动监控测试 ===")
    print(f"阈值: 涨跌幅 > {ALERT_THRESHOLDS['price_change_pct']}% | 成交量 > {ALERT_THRESHOLDS['volume_surge_ratio']}x 均量")
    print(f"扫描间隔: {ALERT_THRESHOLDS['monitor_interval_sec']}秒")

    monitor = IntradayMonitor()
    print(f"\n监控标的: {len(monitor.positions)} 只")
    print(f"均量基线: {len(monitor.baseline)} 只")

    print("\n执行一次扫描...")
    anomalies = monitor.scan()
    print(f"异动数量: {len(anomalies)}")
    for a in anomalies:
        print(f"  {a}")

    if anomalies:
        print("\n推送告警...")
        msg = format_anomaly_message(anomalies)
        print(msg)
        send_notification("⚠️ 盘中异动测试", msg, level="WARNING")
