# -*- coding: utf-8 -*-
"""自测：生成样例文档，验证解析与三条规则判定（不需要飞书凭证和网络）"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from docx import Document

from fsaudit.docparse import parse_doc
from fsaudit.rules import audit_text, load_ai_terms, split_keywords, col_letter

FAILURES = []


def check(name, cond, detail=""):
    tag = "PASS" if cond else "FAIL"
    print("  [%s] %s %s" % (tag, name, ("- " + detail) if detail and not cond else ""))
    if not cond:
        FAILURES.append(name)


def make_docx(path, title, body):
    d = Document()
    d.add_paragraph(title)
    for p in body:
        d.add_paragraph(p)
    d.save(path)


def main():
    tmp = tempfile.mkdtemp()
    terms = load_ai_terms()

    print("== 解析 + 规则自测 ==")

    # 1. 应通过：我方品牌先出现，无AI痕迹、无违禁词
    p1 = os.path.join(tmp, "pass.docx")
    make_docx(p1, "蓝海新品发布", ["蓝海新品今天正式发布。", "性能全面领先。", "友商竞品X也在跟进。"])
    title, text = parse_doc(p1)
    res = audit_text(text, split_keywords("蓝海, 蓝海科技"), terms, ["AI生成", "免责声明"], ["竞品X"])
    check("规则全过样例", res["passed"], str(res["reasons"]))

    # 2. 规则一不通过：出现 DeepSeek
    p2 = os.path.join(tmp, "r1.docx")
    make_docx(p2, "蓝海体验", ["蓝海新品体验很好。", "据说 DeepSeek 也赞不绝口。"])
    _, text = parse_doc(p2)
    res = audit_text(text, ["蓝海"], terms, ["AI生成", "免责声明"], [])
    check("规则一命中DeepSeek", (not res["passed"]) and any("规则一" in r and "deepseek" in r.lower() for r in res["reasons"]), str(res["reasons"]))

    # 3. 规则二不通过：出现「免责声明」和「AI生成」
    p3 = os.path.join(tmp, "r2.docx")
    make_docx(p3, "蓝海介绍", ["蓝海公司介绍。", "本AI生成内容仅供参考。", "免责声明：本文不构成建议。"])
    _, text = parse_doc(p3)
    res = audit_text(text, ["蓝海"], terms, ["AI生成", "免责声明"], [])
    hits = [r for r in res["reasons"] if "规则二" in r]
    check("规则二双命中", (not res["passed"]) and len(hits) == 1 and "AI生成" in hits[0] and "免责声明" in hits[0], str(res["reasons"]))

    # 4. 规则三不通过：竞品先出现
    p4 = os.path.join(tmp, "r3.docx")
    make_docx(p4, "竞品X对比评测", ["竞品X最近很火。", "对比来看蓝海更胜一筹。"])
    _, text = parse_doc(p4)
    res = audit_text(text, ["蓝海"], terms, ["AI生成", "免责声明"], ["竞品X"])
    check("规则三竞品先现", (not res["passed"]) and any("规则三" in r for r in res["reasons"]), str(res["reasons"]))

    # 5. 规则三：我方关键词未出现
    p5 = os.path.join(tmp, "r3b.docx")
    make_docx(p5, "随便一篇文章", ["这是一篇没有提到品牌的文章。"])
    _, text = parse_doc(p5)
    res = audit_text(text, ["蓝海"], terms, ["AI生成", "免责声明"], [])
    check("规则三我方未出现", (not res["passed"]) and any("未在文中出现" in r for r in res["reasons"]), str(res["reasons"]))

    # 6. 多关键词：任一我方词先于竞品即可
    p6 = os.path.join(tmp, "r3c.docx")
    make_docx(p6, "文章", ["蓝海科技发布了新品。", "竞品X随后跟进。"])
    _, text = parse_doc(p6)
    res = audit_text(text, split_keywords("蓝海、蓝海科技"), terms, ["AI生成", "免责声明"], ["竞品X"])
    check("多关键词先现通过", res["passed"], str(res["reasons"]))

    # 7. 品牌名包含关系不算竞品
    p7 = os.path.join(tmp, "r3d.docx")
    make_docx(p7, "文章", ["蓝海集团旗下产品上市。"])
    _, text = parse_doc(p7)
    res = audit_text(text, ["蓝海"], terms, ["AI生成", "免责声明"], [])
    check("包含关系归我方", res["passed"], str(res["reasons"]))

    # 8. 关键词拆分
    check("关键词拆分", split_keywords("蓝海, 蓝海科技、XYZ｜abc") == ["蓝海", "蓝海科技", "XYZ", "abc"])

    # 9. 列号转换
    check("列号转换", col_letter(1) == "A" and col_letter(26) == "Z" and col_letter(27) == "AA" and col_letter(53) == "BA")

    print("")
    if FAILURES:
        print("自测未通过 %d 项：%s" % (len(FAILURES), "、".join(FAILURES)))
        sys.exit(1)
    print("自测全部通过 ✔")


if __name__ == "__main__":
    main()
