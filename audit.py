# -*- coding: utf-8 -*-
"""飞书共享电子表格 · 文章内容审核工具

审核对象：表格「回链」列中挂的 .doc/.docx 附件文档
（其他链接——抖音/淘宝/公众号等发布链接——或空值一律跳过不审）

规则一：AI 痕迹词库（wordbooks/ai_terms.yaml）
规则二：违禁词写死匹配（默认「AI生成」「免责声明」）
规则三：项目关键词首次出现位置必须早于任何竞品品牌
        竞品识别：LLM 识别文章中的品牌实体（配置 api_key 启用）+ 本地词库兜底

用法:
  python audit.py --init          首次配置向导(凭证/表格/LLM)
  python audit.py                 交互式选择工作表并审核
  python audit.py --all           审核所有可识别的工作表
  python audit.py --sheet 9月6日   只审核指定名称的工作表(可多次)
  python audit.py --dry-run       只预览不回写
  python audit.py --force 12 15   强制重审指定行号(忽略已审标记)
"""

import argparse
import getpass
import logging
import os
import sys
import time
from datetime import date

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)

from fsaudit.config import (AppConfig, LLMConfig, load_config, save_config,
                            CONFIG_PATH, parse_spreadsheet_token, DOC_EXTS)
from fsaudit.feishu import Feishu, FeishuError
from fsaudit.docparse import parse_doc, DocParseError
from fsaudit.llm import BrandIdentifier
from fsaudit.rules import (audit_text, load_ai_terms, load_brands,
                           split_keywords, col_letter)

LOG_FILE = os.path.join(BASE_DIR, "audit_%s.log" % date.today().strftime("%Y%m%d"))
CACHE_DIR = os.path.join(BASE_DIR, "cache")


def setup_logging():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[
            logging.FileHandler(LOG_FILE, encoding="utf-8"),
        ],
    )
    return logging.getLogger("audit")


def cmd_init():
    print("=" * 46)
    print("  飞书文章审核工具 · 初始化向导")
    print("=" * 46)

    app_id = input("① 飞书 app_id：").strip()
    app_secret = getpass.getpass("② 飞书 app_secret（输入不回显）：").strip()
    fs = Feishu(app_id, app_secret)
    try:
        fs.token()
    except FeishuError as e:
        print("   ✗ 凭证验证失败：%s" % e)
        return
    print("   ✔ 凭证验证通过，已获取访问令牌")

    link = input("③ 电子表格链接（https://xxx.feishu.cn/sheets/xxx）：").strip()
    try:
        token = parse_spreadsheet_token(link)
    except ValueError as e:
        print("   ✗ %s" % e)
        return
    try:
        sheets = fs.list_sheets(token)
    except FeishuError as e:
        print("   ✗ 读取表格失败：%s（请检查应用权限和表格是否已共享给应用）" % e)
        return
    print("   ✔ 识别成功，共 %d 个工作表：%s"
          % (len(sheets), "、".join(s["title"] for s in sheets)))

    llm = LLMConfig()
    api_key = getpass.getpass("④ LLM API key（直接回车=纯词库模式，规则三仅用本地词库）：").strip()
    if api_key:
        llm.api_key = api_key
        base = input("   base_url [回车=默认 %s]：" % llm.base_url).strip()
        if base:
            llm.base_url = base
        model = input("   模型名 [回车=默认 %s]：" % llm.model).strip()
        if model:
            llm.model = model
        try:
            BrandIdentifier(llm, cache_dir=CACHE_DIR).selfcheck()
        except Exception as e:
            print("   ✗ 模型连通失败：%s" % e)
            return
        print("   ✔ 模型连通正常")
    else:
        print("   （纯词库模式：可在 wordbooks/brands.yaml 中维护竞品词库）")

    cfg = AppConfig(app_id=app_id, app_secret=app_secret,
                    spreadsheet_token=token, llm=llm)
    save_config(cfg)
    print("✔ 配置已保存到 %s" % CONFIG_PATH)
    print("  日常运行：python audit.py")


def pick_sheets(fs, cfg, args):
    """筛选可审核的工作表：需含「项目」列和「回链」列"""
    sheets = fs.list_sheets(cfg.spreadsheet_token)
    cols = cfg.columns
    usable, others = [], []
    for s in sheets:
        try:
            header = fs.read_header(cfg.spreadsheet_token, s["sheet_id"])
        except FeishuError:
            others.append(s)
            continue
        names = set(h for h in header if h)
        if cols["keyword"] in names and cols["link"] in names:
            usable.append(s)
        else:
            others.append(s)
    if others:
        print("（跳过非审核表：%s）" % "、".join(s["title"] for s in others))

    if not usable:
        raise SystemExit("✗ 没有识别出任何含「%s」和「%s」列的工作表，"
                         "请检查表头或 config.yaml 的 columns 配置"
                         % (cols["keyword"], cols["link"]))

    if args.all:
        return usable
    if args.sheet:
        want = set(args.sheet)
        chosen = [s for s in usable if s["title"] in want]
        missing = want - {s["title"] for s in chosen}
        if missing:
            raise SystemExit("✗ 找不到工作表：%s（现有：%s）"
                             % ("、".join(missing), "、".join(s["title"] for s in usable)))
        return chosen

    print("\n可审核的工作表：")
    for i, s in enumerate(usable, 1):
        print("  [%d] %s" % (i, s["title"]))
    raw = input("请选择要审核的工作表（编号，逗号分隔；直接回车=全部）：").strip()
    if not raw:
        return usable
    idxs = []
    for part in raw.replace("，", ",").split(","):
        part = part.strip()
        if part.isdigit() and 1 <= int(part) <= len(usable):
            idxs.append(int(part) - 1)
        else:
            raise SystemExit("✗ 无效编号：%s" % part)
    return [usable[i] for i in idxs]


def run_audit(args):
    if not os.path.exists(CONFIG_PATH):
        raise SystemExit("✗ 未找到配置文件，请先运行：python audit.py --init")
    cfg = load_config()
    log = setup_logging()
    fs = Feishu(cfg.app_id, cfg.app_secret)
    identifier = BrandIdentifier(cfg.llm, cache_dir=CACHE_DIR)
    ai_terms = load_ai_terms()
    brands_lex = load_brands()
    if identifier.available():
        print("（LLM 模式：规则三竞品识别 = LLM 品牌实体识别 + 本地词库兜底）")
    else:
        print("（纯词库模式：未配置 LLM key，规则三使用 wordbooks/brands.yaml 竞品词库）")

    sheets = pick_sheets(fs, cfg, args)
    print("\n待审核工作表：%s%s\n" % ("、".join(s["title"] for s in sheets),
                                   "（仅预览，不回写）" if args.dry_run else ""))

    writebacks = []   # (sheet_id, sheet_title, row_no, res_col_letter, reason_col_letter, result, reason)
    stat = {"pass": 0, "fail": 0, "nondoc": 0, "skipped": 0, "error": 0}

    for s in sheets:
        data = fs.read_all(cfg.spreadsheet_token, s["sheet_id"])
        if not data or len(data) <= 1:
            print("[%s] 空表，跳过" % s["title"])
            continue
        header = [str(c or "").strip() for c in data[0]]
        try:
            c_kw = header.index(cfg.columns["keyword"])
        except ValueError:
            print("[%s] 缺「%s」列，跳过" % (s["title"], cfg.columns["keyword"]))
            continue
        try:
            c_link = header.index(cfg.columns["link"])
        except ValueError:
            print("[%s] 缺「%s」列，跳过" % (s["title"], cfg.columns["link"]))
            continue
        c_res = header.index(cfg.columns["result"]) if cfg.columns["result"] in header else None
        c_reason = header.index(cfg.columns["reason"]) if cfg.columns["reason"] in header else None
        if c_res is None:
            print("[%s] ⚠ 缺「%s」列，本表只能预览不能回写" % (s["title"], cfg.columns["result"]))

        # 回链列单独用公式计算值模式读取（拿附件 fileToken）
        link_letter = col_letter(c_link + 1)
        link_col = fs.read_range(cfg.spreadsheet_token,
                                  "%s!%s1:%s%d" % (s["sheet_id"], link_letter, link_letter, len(data)),
                                  formula=True)

        todo = []
        for i, row in enumerate(data[1:], start=2):
            row = list(row) + [""] * max(0, len(header) - len(row))
            kw = str(row[c_kw] or "").strip()
            link_val = link_col[i - 1][0] if i - 1 < len(link_col) else ""
            # 提取 .doc/.docx 附件；其他链接/空值/非文档附件一律跳过
            atts = fs.cell_attachments(link_val)
            doc_atts = [(ft, name) for ft, name in atts
                        if os.path.splitext(name)[1].lower() in DOC_EXTS]
            if not doc_atts:
                stat["nondoc"] += 1   # 空值 / 发布链接 / 非文档附件，均不审核
                continue
            if c_res is not None:
                done = str(row[c_res] or "").strip()
                if done and not (args.force and i in args.force):
                    stat["skipped"] += 1
                    continue
            todo.append((i, kw, doc_atts))
        if not todo:
            print("[%s] 没有待审文档行（已审 %d · 非文档跳过 %d）"
                  % (s["title"], stat["skipped"], stat["nondoc"]))
            continue

        print("[%s] 待审文档 %d 个" % (s["title"], len(todo)))
        for row_no, kw, doc_atts in todo:
            file_token, filename = doc_atts[0]
            keywords = split_keywords(kw)
            # 下载并解析文档
            local = os.path.join(CACHE_DIR, "%s_%s" % (s["sheet_id"], filename))
            try:
                fs.download(file_token, local)
                title, content = parse_doc(local)
            except (FeishuError, DocParseError) as e:
                stat["error"] += 1
                print("  [行%d] %s -> 下载/解析失败：%s" % (row_no, filename[:40], e))
                log.error("[%s][行%d] %s 下载/解析失败 %s", s["title"], row_no, filename, e)
                if c_res is not None:
                    writebacks.append((s["sheet_id"], s["title"], row_no,
                                       col_letter(c_res + 1),
                                       col_letter(c_reason + 1) if c_reason is not None else None,
                                       "不通过", "文档下载或解析失败：%s" % e))
                continue
            text = (title + "\n" + content).strip() if title else content

            # 竞品识别：LLM + 本地词库
            try:
                brands = identifier.identify(text)
            except Exception as e:
                log.warning("LLM 识别失败（%s），退化为词库模式", e)
                brands = []
            lex = [b for b in brands_lex if b.lower() in text.lower()]
            all_brands = list(dict.fromkeys(brands + lex))
            competitors = [b for b in all_brands
                           if not any(b in k or k in b for k in keywords)]

            res = audit_text(text, keywords, ai_terms, cfg.forbidden_words, competitors)
            reason = "；".join(res["reasons"])
            result_text = "通过" if res["passed"] else "不通过"
            if res["passed"]:
                stat["pass"] += 1
            else:
                stat["fail"] += 1
            print("  [行%d] %s 项目=%s -> %s"
                  % (row_no, (filename or "")[:40], kw or "（空）", result_text))
            for r in (res["reasons"] or []):
                print("        ✗ %s" % r)
            log.info("[%s][行%d] %s -> %s %s", s["title"], row_no, filename, result_text, reason or "")
            if c_res is not None:
                writebacks.append((s["sheet_id"], s["title"], row_no,
                                   col_letter(c_res + 1),
                                   col_letter(c_reason + 1) if c_reason is not None else None,
                                   result_text, reason))

    print("\n汇总（%s）：通过 %d · 不通过 %d · 非文档跳过 %d · 跳过已审 %d · 下载解析失败 %d"
          % ("预览" if args.dry_run else "待回写",
             stat["pass"], stat["fail"], stat["nondoc"], stat["skipped"], stat["error"]))
    if not writebacks:
        print("没有需要回写的结果。")
        return
    if args.dry_run:
        print("dry-run 模式：未回写。去掉 --dry-run 并确认后才会写入表格。")
        return

    if input("确认回写飞书表格？(y/n)：").strip().lower() != "y":
        print("已取消，未回写。")
        return
    ok = 0
    for sheet_id, title, row_no, res_letter, reason_letter, result_text, reason in writebacks:
        token = cfg.spreadsheet_token
        try:
            fs.write_cell(token, sheet_id, res_letter + str(row_no), result_text)
            if reason_letter:
                fs.write_cell(token, sheet_id, reason_letter + str(row_no), reason or "")
            ok += 1
        except FeishuError as e:
            print("  ✗ [%s 行%d] 回写失败：%s" % (title, row_no, e))
            print("  （提示：请把表格分享给应用时权限设为「可编辑」）")
            break
        time.sleep(0.15)
    print("✔ 已回写 %d 行，日志：%s" % (ok, LOG_FILE))


def main():
    ap = argparse.ArgumentParser(description="飞书共享电子表格 · 文章内容审核工具")
    ap.add_argument("--init", action="store_true", help="首次配置向导")
    ap.add_argument("--sheet", action="append", default=[], help="指定工作表名称，可多次")
    ap.add_argument("--all", action="store_true", help="审核全部可识别工作表")
    ap.add_argument("--dry-run", action="store_true", help="只预览不回写")
    ap.add_argument("--force", type=int, nargs="+", default=[], help="强制重审指定行号")
    args = ap.parse_args()
    if args.init:
        cmd_init()
    else:
        run_audit(args)


if __name__ == "__main__":
    main()
