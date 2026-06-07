"""
情绪因子量化 — 新闻/公告情感分析
使用关键词匹配进行基础情感分析（无需外部API）
"""

POSITIVE_KEYWORDS = [
    "增持", "买入", "推荐", "看好", "超预期", "突破", "创新高",
    "业绩增长", "利润增长", "订单大增", "签约", "合作", "扩张",
    "评级上调", "目标价", "首次覆盖", "强烈推荐", "优于大市"
]

NEGATIVE_KEYWORDS = [
    "减持", "卖出", "下调", "预警", "风险", "亏损", "业绩下滑",
    "债务风险", "诉讼", "调查", "处罚", "裁员", "终止",
    "降级", "跑输大市", "目标价下调", "商誉减值", "资产减值"
]


def analyze_sentiment(text: str) -> dict:
    """
    分析文本情感，返回 score (-1~1) 和标签

    Args:
        text: 待分析的文本内容

    Returns:
        包含 score (-1~1), label (positive/negative/neutral), confidence (0~1),
        pos_count, neg_count 的字典
    """
    if not text:
        return {"score": 0.0, "label": "neutral", "confidence": 0.0}
    pos_count = sum(1 for kw in POSITIVE_KEYWORDS if kw in text)
    neg_count = sum(1 for kw in NEGATIVE_KEYWORDS if kw in text)
    total = pos_count + neg_count
    if total == 0:
        return {"score": 0.0, "label": "neutral", "confidence": 0.0}
    score = (pos_count - neg_count) / total  # -1 to 1
    return {
        "score": score,
        "label": "positive" if score > 0.1 else "negative" if score < -0.1 else "neutral",
        "confidence": min(total / 5, 1.0),
        "pos_count": pos_count,
        "neg_count": neg_count,
    }


def batch_analyze(texts: list[str]) -> list[dict]:
    """批量分析文本列表"""
    return [analyze_sentiment(t) for t in texts]


if __name__ == "__main__":
    # 验证测试
    test_texts = [
        "公司业绩超预期，评级上调至增持",
        "公司业绩下滑，评级下调至减持",
        "公司公告称生产经营正常",
    ]
    for t in test_texts:
        result = analyze_sentiment(t)
        print(f"[{t[:20]}...] score={result['score']:.2f} label={result['label']} conf={result['confidence']:.2f}")