# -*- coding: utf-8 -*-
"""文档解析：.docx 直接解析，.doc 经 Word/WPS COM 转换后解析"""
import os

from docx import Document as DocxDocument


class DocParseError(Exception):
    pass


def _docx_text(path):
    d = DocxDocument(path)
    parts = [p.text.strip() for p in d.paragraphs if p.text.strip()]
    for table in d.tables:
        for row in table.rows:
            for cell in row.cells:
                t = cell.text.strip()
                if t:
                    parts.append(t)
    if not parts:
        raise DocParseError("文档内容为空")
    title = parts[0]
    return title, "\n".join(parts)


_CONVERT_TIMEOUT = 90  # 秒：.doc 转换最长等待，超时熔断跳过该行（防止 WPS/Word 弹窗卡死审核线程）


def _doc_to_docx(src):
    """用子进程 + 超时熔断将 .doc 转为 .docx。

    .doc 必须经 Word/WPS COM 转换；COM 调用阻塞且无法被 Python 中断，
    若弹窗（受保护视图/文件占用/首次运行向导）会无限挂起。放子进程执行，
    超时即 taskkill 整棵进程树并抛 DocParseError，由调用方跳过该行继续。
    """
    import subprocess
    import sys as _sys
    dst = os.path.abspath(src + "x")
    helper = os.path.join(os.path.dirname(os.path.abspath(__file__)), "doc_convert.py")
    if not os.path.exists(helper):
        raise DocParseError(".doc 转换助手缺失：%s" % helper)
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        proc = subprocess.Popen(
            [_sys.executable, helper, os.path.abspath(src), dst],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            creationflags=flags)
    except Exception as e:
        raise DocParseError(".doc 转换启动失败：%s" % e)
    try:
        _, err = proc.communicate(timeout=_CONVERT_TIMEOUT)
    except subprocess.TimeoutExpired:
        proc.kill()
        try:  # 连坐杀掉 COM 派生的 Word/WPS 进程，避免残留弹窗
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                           capture_output=True, timeout=15)
        except Exception:
            pass
        raise DocParseError(
            ".doc 转换超时（%ss）：疑似 Word/WPS 弹窗阻塞，已跳过该行。"
            "请手动将该文档另存为 .docx 后再审" % _CONVERT_TIMEOUT)
    if proc.returncode != 0:
        msg = (err or b"").decode("utf-8", "replace").strip()
        raise DocParseError(".doc 转换失败：%s" % (msg or "未知错误"))
    if not os.path.exists(dst):
        raise DocParseError(".doc 转换失败：未生成目标文件")
    return dst


def parse_doc(path):
    """返回 (标题, 标题+正文全文)"""
    ext = os.path.splitext(path)[1].lower()
    if ext == ".docx":
        return _docx_text(path)
    if ext == ".doc":
        converted = _doc_to_docx(path)
        try:
            return _docx_text(converted)
        finally:
            try:
                os.remove(converted)
            except OSError:
                pass
    raise DocParseError("不支持的文件格式：%s" % ext)
