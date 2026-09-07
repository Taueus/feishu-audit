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


def _doc_to_docx(src):
    """用本机 Word 或 WPS 将 .doc 转为 .docx"""
    try:
        import win32com.client
    except ImportError:
        raise DocParseError("未安装 pywin32，无法转换 .doc；请运行 pip install pywin32")
    dst = os.path.abspath(src + "x")
    word = None
    for prog in ("Word.Application", "KWPS.Application"):
        try:
            word = win32com.client.DispatchEx(prog)
            break
        except Exception:
            continue
    if word is None:
        raise DocParseError("本机未检测到 Word / WPS，无法处理 .doc 文件，请先转换为 .docx")
    try:
        word.Visible = False
        word.DisplayAlerts = 0
        doc = word.Documents.Open(os.path.abspath(src), ReadOnly=True)
        doc.SaveAs2(dst, FileFormat=16)  # 16 = wdFormatXMLDocument (.docx)
        doc.Close(False)
    finally:
        word.Quit()
    if not os.path.exists(dst):
        raise DocParseError(".doc 转换失败")
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
