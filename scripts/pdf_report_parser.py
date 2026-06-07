"""
本地PDF研报解析器 — 使用marker-pdf提取文本
将PDF转为Markdown后供后续TAMF章节填充
"""

import subprocess
import logging
from pathlib import Path
import tempfile
import json

logger = logging.getLogger("pdf_parser")

def extract_text_from_pdf(pdf_path: str) -> str:
    """
    使用marker-pdf将PDF转为Markdown文本
    返回提取的文本内容
    """
    output_dir = Path(tempfile.mkdtemp())
    
    try:
        result = subprocess.run(
            [
                "python3", "-m", "marker.convert",
                pdf_path,
                "--output_dir", str(output_dir),
                "--parallel_factor", "2",
                "--dont_batch",  # 单文件模式
            ],
            capture_output=True,
            text=True,
            timeout=120,
        )
        
        if result.returncode != 0:
            logger.warning(f"marker-pdf失败: {result.stderr}")
            return ""
        
        # 找到输出的markdown文件
        md_files = list(output_dir.glob("*.md"))
        if not md_files:
            # 尝试txt
            txt_files = list(output_dir.glob("*.txt"))
            if txt_files:
                return txt_files[0].read_text(encoding="utf-8", errors="ignore")
            return ""
        
        content = md_files[0].read_text(encoding="utf-8", errors="ignore")
        return content
    
    except subprocess.TimeoutExpired:
        logger.error(f"marker-pdf超时: {pdf_path}")
        return ""
    except Exception as e:
        logger.error(f"marker-pdf异常: {e}")
        return ""
    finally:
        # 清理临时目录
        import shutil
        shutil.rmtree(output_dir, ignore_errors=True)

def parse_report_to_tamf_section(pdf_path: str) -> dict:
    """
    解析研报PDF，返回适合写入TAMF的结构化数据
    {
        "title": str,
        "summary": str,  # 摘要/核心观点
        "company": str,  # 涉及公司
        "industry": str, # 所属行业
        "risk_factors": list[str],  # 风险因素
        "investment_ideas": list[str],  # 投资观点
    }
    """
    content = extract_text_from_pdf(pdf_path)
    if not content:
        return {}
    
    # 简单关键词提取（后续可接LLM做总结）
    lines = [line.strip() for line in content.split("\n") if line.strip()]
    
    title = lines[0] if lines else ""
    
    # 提取风险因素（关键词匹配）
    risk_keywords = ["风险", "不确定性", "政策风险", "市场风险"]
    risk_factors = [ln for ln in lines if any(k in ln for k in risk_keywords)][:5]
    
    # 提取投资观点
    idea_keywords = ["看好", "推荐", "买入", "增持", "投资价值", "机会"]
    investment_ideas = [ln for ln in lines if any(k in ln for k in idea_keywords)][:5]
    
    # 摘要取前10行
    summary = "\n".join(lines[1:11])
    
    return {
        "title": title,
        "summary": summary,
        "risk_factors": risk_factors,
        "investment_ideas": investment_ideas,
        "raw_length": len(content),
    }

def batch_parse_reports(report_dir: str) -> list[dict]:
    """批量解析目录下的PDF研报"""
    from pathlib import Path
    dir_path = Path(report_dir)
    pdfs = list(dir_path.glob("**/*.pdf"))
    
    results = []
    for pdf in pdfs:
        try:
            result = parse_report_to_tamf_section(str(pdf))
            result["file"] = str(pdf)
            results.append(result)
        except Exception as e:
            logger.error(f"解析失败 {pdf}: {e}")
    
    return results

if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1:
        result = parse_report_to_tamf_section(sys.argv[1])
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print("用法: python3 pdf_report_parser.py <pdf_path>")