# -*- coding: utf-8 -*-
"""子进程 .doc→.docx 转换助手（由 docparse.py 以 subprocess 调用，支持超时熔断）。

为什么单独放子进程：.doc 需经 Word/WPS COM 转换，COM 调用是阻塞的，
若 Word/WPS 弹窗（受保护视图/文件占用/首次运行向导等）会无限期挂起，
主线程无法中断。放入子进程后，父进程可用 timeout 强杀，避免审核线程卡死。

用法: python doc_convert.py <src.doc> <dst.docx>
退出码 0 = 成功；非 0 = 失败（错误信息输出到 stderr）。
"""
import os
import sys


def convert(src, dst):
    try:
        import win32com.client
    except ImportError:
        return "未安装 pywin32，无法转换 .doc（pip install pywin32）"
    word = None
    for prog in ("Word.Application", "KWPS.Application"):
        try:
            word = win32com.client.DispatchEx(prog)
            break
        except Exception:
            continue
    if word is None:
        return "本机未检测到 Word / WPS，无法处理 .doc 文件"
    try:
        word.Visible = False
        word.DisplayAlerts = 0
        doc = word.Documents.Open(os.path.abspath(src), ReadOnly=True)
        doc.SaveAs2(dst, FileFormat=16)  # 16 = wdFormatXMLDocument (.docx)
        doc.Close(False)
    except Exception as e:
        return "转换异常：%s" % e
    finally:
        try:
            word.Quit()
        except Exception:
            pass
    return "" if os.path.exists(dst) else "转换后未生成文件"


if __name__ == "__main__":
    if len(sys.argv) != 3:
        sys.stderr.write("usage: doc_convert.py <src.doc> <dst.docx>\n")
        sys.exit(2)
    err = convert(sys.argv[1], sys.argv[2])
    if err:
        sys.stderr.write(err + "\n")
        sys.exit(1)
