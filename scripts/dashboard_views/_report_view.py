"""研报阅读视图 — 研报阅读页"""

from ._news import render_reports


def get_active_view_name() -> str:
    return "report"


def render():
    """渲染研报阅读页面"""
    # 调用现有的研报渲染函数
    render_reports()