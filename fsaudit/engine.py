# -*- coding: utf-8 -*-
"""可复用审核引擎（供机器人 / 其他界面调用，与 CLI 的 audit.py 逻辑一致）

用法:
    engine = AuditEngine(cfg)
    sheets = engine.list_sheets()          # [{sheet_id, title, usable}]
    run = engine.run(sheet_ids, dry_run, force, on_event=cb)
    engine.write_back(run, on_event=cb)

事件(on_event 回调参数，均为 dict):
    {"type": "log",         "msg": str}
    {"type": "sheet_start", "sheet": str}
    {"type": "sheet_docs",  "sheet": str, "total": int}
    {"type": "row_start",   "sheet": str, "row": int, "file": str, "index": int, "total": int}
    {"type": "row_done",    "sheet": str, "row": int, "file": str, "kw": str,
                            "passed": bool, "reasons": [str]}
"""
import logging
import os
import time
from datetime import date

from .config import AppConfig, load_config, DOC_EXTS
from .feishu import Feishu, FeishuError
from .docparse import parse_doc, DocParseError
from .llm import BrandIdentifier
from .rules import load_ai_terms, load_brands, split_keywords, col_letter, audit_text
from .viewpoint import ViewpointAuditor, format_issues
from .ai_quality import AIQualityAuditor, format_hard_reasons, format_suggestions

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CACHE_DIR = os.path.join(BASE_DIR, "cache")


def setup_file_logging():
    log_file = os.path.join(BASE_DIR, "audit_%s.log" % date.today().strftime("%Y%m%d"))
    logger = logging.getLogger("audit")
    if not logger.handlers:
        logger.setLevel(logging.INFO)
        logger.addHandler(logging.FileHandler(log_file, encoding="utf-8"))
    return logger


class AuditEngine(object):
    """一次实例 = 一套凭证 + 一张表格；run() 可多次调用"""

    def __init__(self, cfg=None, cache_dir=None):
        self.cfg = cfg or load_config()
        self.cache_dir = cache_dir or CACHE_DIR
        os.makedirs(self.cache_dir, exist_ok=True)
        self.fs = Feishu(self.cfg.app_id, self.cfg.app_secret)
        self.identifier = BrandIdentifier(self.cfg.llm, cache_dir=self.cache_dir)
        self.viewpoint = ViewpointAuditor(self.cfg.llm, cache_dir=self.cache_dir)
        self.ai_quality = AIQualityAuditor(self.cfg.llm, cache_dir=self.cache_dir)
        self.log = setup_file_logging()

    # ------------------------------------------------ 列工作表

    def list_sheets(self):
        """返回 [{sheet_id, title, usable}]，usable=含「项目」和「回链」列"""
        token = self.cfg.spreadsheet_token
        cols = self.cfg.columns
        out = []
        for s in self.fs.list_sheets(token):
            usable = False
            try:
                header = self.fs.read_header(token, s["sheet_id"])
                names = set(h for h in header if h)
                usable = cols["keyword"] in names and cols["link"] in names
            except FeishuError:
                usable = False
            out.append({"sheet_id": s["sheet_id"], "title": s["title"], "usable": usable})
        return out

    # ------------------------------------------------ 执行审核

    def run(self, sheet_ids, dry_run=False, force=False, on_event=None):
        """审核选定工作表，返回 run dict（含待回写清单，回写需另行调用 write_back）"""
        emit = on_event or (lambda ev: None)
        cfg = self.cfg
        token = cfg.spreadsheet_token
        ai_terms = load_ai_terms()
        brands_lex = load_brands()

        want = set(sheet_ids)
        chosen = [s for s in self.fs.list_sheets(token) if s["sheet_id"] in want]

        stat = {"pass": 0, "fail": 0, "nondoc": 0, "skipped": 0, "error": 0}
        results = []
        writebacks = []   # (sheet_id, sheet_title, row_no, res_letter, reason_letter, result, reason)

        for s in chosen:
            emit({"type": "sheet_start", "sheet": s["title"]})
            data = self.fs.read_all(token, s["sheet_id"])
            if not data or len(data) <= 1:
                emit({"type": "log", "msg": "[%s] 空表，跳过" % s["title"]})
                continue
            header = [str(c or "").strip() for c in data[0]]
            try:
                c_kw = header.index(cfg.columns["keyword"])
            except ValueError:
                emit({"type": "log", "msg": "[%s] 缺「%s」列，跳过"
                      % (s["title"], cfg.columns["keyword"])})
                continue
            try:
                c_link = header.index(cfg.columns["link"])
            except ValueError:
                emit({"type": "log", "msg": "[%s] 缺「%s」列，跳过"
                      % (s["title"], cfg.columns["link"])})
                continue
            c_res = header.index(cfg.columns["result"]) if cfg.columns["result"] in header else None
            c_reason = header.index(cfg.columns["reason"]) if cfg.columns["reason"] in header else None
            if c_res is None:
                emit({"type": "log", "msg": "[%s] ⚠ 缺「%s」列，本表只能预览不能回写"
                      % (s["title"], cfg.columns["result"])})

            # 回链列公式计算值模式读取（拿附件 fileToken）
            link_letter = col_letter(c_link + 1)
            link_col = self.fs.read_range(token,
                                          "%s!%s1:%s%d" % (s["sheet_id"], link_letter, link_letter, len(data)),
                                          formula=True)

            todo = []
            for i, row in enumerate(data[1:], start=2):
                row = list(row) + [""] * max(0, len(header) - len(row))
                kw = str(row[c_kw] or "").strip()
                link_val = link_col[i - 1][0] if i - 1 < len(link_col) else ""
                atts = self.fs.cell_attachments(link_val)
                doc_atts = [(ft, name) for ft, name in atts
                            if os.path.splitext(name)[1].lower() in DOC_EXTS]
                if not doc_atts:
                    stat["nondoc"] += 1
                    continue
                if c_res is not None and not force:
                    done = str(row[c_res] or "").strip()
                    if done:
                        stat["skipped"] += 1
                        continue
                todo.append((i, kw, doc_atts))

            if not todo:
                emit({"type": "log", "msg": "[%s] 没有待审文档行（已审 %d · 非文档跳过 %d）"
                      % (s["title"], stat["skipped"], stat["nondoc"])})
                continue

            emit({"type": "sheet_docs", "sheet": s["title"], "total": len(todo)})
            for idx, (row_no, kw, doc_atts) in enumerate(todo, 1):
                file_token, filename = doc_atts[0]
                emit({"type": "row_start", "sheet": s["title"], "row": row_no,
                      "file": filename, "index": idx, "total": len(todo)})
                keywords = split_keywords(kw)
                local = os.path.join(self.cache_dir, "%s_%s" % (s["sheet_id"], filename))
                try:
                    self.fs.download(file_token, local)
                    title, content = parse_doc(local)
                except (FeishuError, DocParseError) as e:
                    stat["error"] += 1
                    reasons = ["文档下载或解析失败：%s" % e]
                    self.log.error("[%s][行%d] %s 下载/解析失败 %s", s["title"], row_no, filename, e)
                    results.append({"sheet": s["title"], "row": row_no, "file": filename,
                                    "kw": kw, "passed": False, "reasons": reasons})
                    emit({"type": "row_done", "sheet": s["title"], "row": row_no,
                          "file": filename, "kw": kw, "passed": False, "reasons": reasons})
                    if c_res is not None:
                        writebacks.append((s["sheet_id"], s["title"], row_no,
                                           col_letter(c_res + 1),
                                           col_letter(c_reason + 1) if c_reason is not None else None,
                                           "不通过", "；".join(reasons)))
                    continue

                text = (title + "\n" + content).strip() if title else content

                # 竞品识别：LLM + 本地词库
                try:
                    brands = self.identifier.identify(text)
                except Exception as e:
                    self.log.warning("LLM 识别失败（%s），退化为词库模式", e)
                    brands = []
                lex = [b for b in brands_lex if b.lower() in text.lower()]
                all_brands = list(dict.fromkeys(brands + lex))
                competitors = [b for b in all_brands
                               if not any(b in k or k in b for k in keywords)]

                res = audit_text(text, keywords, ai_terms, cfg.forbidden_words,
                                 competitors, cfg.rules_enabled)
                reasons = list(res["reasons"])
                # 规则四 · 观点级主角性（LLM）：仅当前三条确定性规则通过、
                # 我方关键词确在文中出现、识别到竞品、且规则四开关打开时调用
                r4_on = cfg.rules_enabled.get("r4", True)
                if (not reasons and competitors and r4_on
                        and self.viewpoint.available()
                        and any((k or "").strip() and k.lower() in text.lower() for k in keywords)):
                    try:
                        vp = self.viewpoint.audit(text, keywords, competitors)
                        if vp.get("issues"):
                            txt4 = format_issues(vp["issues"])
                            if txt4:
                                reasons.append("规则四：观点级主角性——%s" % txt4)
                    except Exception as e:
                        self.log.warning("规则四 LLM 判定失败（行%d），本规则跳过：%s",
                                         row_no, e)
                # 规则五 · AI 人味 & AI 收录友好度（LLM + 本地启发式）：
                # 总开关由 cfg.rules_enabled["r5"] 控制；未配置 Key 时静默跳过。
                # 仅在我方关键词确在文中出现时判定（无关键词则无可抽取断言可言）。
                r5_on = cfg.rules_enabled.get("r5", True)
                if (r5_on and self.ai_quality.available()
                        and any((k or "").strip() and k.lower() in text.lower() for k in keywords)):
                    try:
                        aq = self.ai_quality.audit(text, keywords)
                        # 硬性一票否决：理由前置到 reasons 头部，让作者先看硬伤
                        if aq.get("hard_reasons"):
                            hard_txt = format_hard_reasons(aq["hard_reasons"])
                            reasons.insert(0, "规则五：%s" % hard_txt)
                        # 收录建议：仅在原因为空（仍为通过）时附加，避免与硬性混排
                        elif aq.get("suggestions"):
                            sug_txt = format_suggestions(aq["suggestions"])
                            if sug_txt:
                                reasons.append("规则五·收录优化建议——%s" % sug_txt)
                    except Exception as e:
                        self.log.warning("规则五 LLM 判定失败（行%d），本规则跳过：%s",
                                         row_no, e)
                passed = not reasons
                reason = "；".join(reasons)
                result_text = "通过" if passed else "不通过"
                if passed:
                    stat["pass"] += 1
                else:
                    stat["fail"] += 1
                self.log.info("[%s][行%d] %s -> %s %s", s["title"], row_no,
                              filename, result_text, reason)
                results.append({"sheet": s["title"], "row": row_no, "file": filename,
                                "kw": kw, "passed": passed, "reasons": reasons})
                emit({"type": "row_done", "sheet": s["title"], "row": row_no,
                      "file": filename, "kw": kw, "passed": passed,
                      "reasons": reasons})
                if c_res is not None:
                    writebacks.append((s["sheet_id"], s["title"], row_no,
                                       col_letter(c_res + 1),
                                       col_letter(c_reason + 1) if c_reason is not None else None,
                                       result_text, reason))

        return {"dry_run": dry_run, "stat": stat, "results": results,
                "writebacks": writebacks}

    # ------------------------------------------------ 回写

    def write_back(self, run, on_event=None):
        """把 run 中的待回写结果写入飞书表格，返回 {ok, failures}"""
        emit = on_event or (lambda ev: None)
        token = self.cfg.spreadsheet_token
        ok, failures = 0, []
        for sheet_id, title, row_no, res_letter, reason_letter, result_text, reason in run["writebacks"]:
            try:
                self.fs.write_cell(token, sheet_id, res_letter + str(row_no), result_text)
                if reason_letter:
                    self.fs.write_cell(token, sheet_id, reason_letter + str(row_no), reason or "")
                ok += 1
                emit({"type": "log", "msg": "已回写 [%s 行%d] %s" % (title, row_no, result_text)})
            except FeishuError as e:
                failures.append({"sheet": title, "row": row_no, "error": str(e)})
                emit({"type": "log", "msg": "回写失败 [%s 行%d]：%s" % (title, row_no, e)})
                break
            time.sleep(0.15)
        return {"ok": ok, "failures": failures}
