# InvestPilot 编码规范

## 1. 函数注释
- 每个公开函数必须有 docstring（中文）
- 示例：
```python
def get_data(code: str) -> dict:
    """
    获取指定代码的市场数据。
    Args:
        code: 股票代码（6位）
    Returns:
        含OHLCV的字典，失败返回空dict
    """
```

## 2. 命名规范
- 函数/变量: snake_case
- 类名: PascalCase
- 常量: UPPER_SNAKE_CASE
- 私有成员: 前缀下划线_

## 3. 错误处理（强制）
- 禁止 bare except → 必须 `except Exception:`
- 禁止吞掉异常 → except中必须记录日志或返回错误信息
- 外部依赖调用必须有降级路径（try/except + fallback）

## 4. 日志规范
- ERROR: 系统级错误（不可恢复）
- WARNING: 降级发生（可恢复）
- INFO: 关键节点（人工需关注）
- DEBUG: 详细调试信息

## 5. 类型注解
- 公开函数参数和返回值建议添加类型注解
- 复杂泛型可选

## 6. 代码组织
- 公共函数提取到 scripts/utils/
- 避免重复代码（DRY原则）
- 每个模块不超过500行

## 7. Streamlit特殊要求
- st.set_page_config()必须第一行
- 所有st调用必须在函数内（render_xxx）
- 不在顶层放耗时计算