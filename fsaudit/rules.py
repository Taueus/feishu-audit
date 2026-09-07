# -*- coding: utf-8 -*-
"""审核规则引擎

规则一：AI 痕迹词库匹配（wordbooks/ai_terms.yaml）
规则二：写死违禁词精确匹配（默认「AI生成」「免责声明」）
规则三：我方关键词首次出现位置必须早于任何竞品
（规则四 · 观点级主角性为 LLM 判定，见 fsaudit/viewpoint.py，由 engine 调用）
"""
import os
import re

import yaml

WORDBOOK_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "wordbooks")


def _read_yaml(name, key):
    path = os.path.join(WORDBOOK_DIR, name)
    if not os.path.exists(path):
        return []
    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    return [str(x).strip() for x in (data.get(key) or []) if str(x).strip()]


def load_ai_terms():
    terms = _read_yaml("ai_terms.yaml", "terms")
    seen, out = set(), []
    for t in terms:
        k = t.lower()
        if k not in seen:
            seen.add(k)
            out.append(t)
    return out


def load_brands():
    return _read_yaml("brands.yaml", "known_brands")


def split_keywords(cell):
    """把「项目」单元格拆成多个关键词"""
    cell = (cell or "").strip()
    if not cell:
        return []
    parts = [p for p in re.split(r"[,，、;；/|｜\s]+", cell) if p]
    return parts or [cell]


def rule1_hits(text, terms):
    t = (text or "").lower()
    return [term for term in terms if term.lower() in t]


def rule2_hits(text, words):
    t = (text or "").lower()
    return [w for w in words if w.lower() in t]


def is_mine(brand, keywords):
    return any(brand in k or k in brand for k in keywords)


def rule3_check(text, keywords, competitors):
    """返回 (通过?, 原因)"""
    if not keywords:
        return False, "规则三：「项目」字段未填写关键词"
    t = (text or "").lower()
    mine_idx = [t.find(k.lower()) for k in keywords]
    found = [i for i in mine_idx if i >= 0]
    if not found:
        return False, "规则三：我方关键词（%s）未在文中出现" % "、".join(keywords)
    my_first = min(found)
    earlier = [b for b in competitors if 0 <= t.find(b.lower()) < my_first]
    if earlier:
        return False, "规则三：竞品「%s」首次出现早于我方关键词" % "、".join(earlier)
    return True, None


def audit_text(text, keywords, ai_terms, forbidden_words, competitors, rules_enabled=None):
    """执行确定性审核规则（规则一二三），返回 {"passed": bool, "reasons": [str]}
    rules_enabled: dict，键为 r1/r2/r3；缺省视为全开。
    规则四/五（LLM 判定）由 engine 另行调用并按 r4/r5 开关决定是否触发。"""
    enabled = {"r1": True, "r2": True, "r3": True}
    if rules_enabled:
        for k in ("r1", "r2", "r3"):
            if k in rules_enabled:
                enabled[k] = bool(rules_enabled[k])
    reasons = []
    if enabled["r1"]:
        r1 = rule1_hits(text, ai_terms)
        if r1:
            reasons.append("规则一：检测到AI痕迹词「%s」" % "、".join(r1))
    if enabled["r2"]:
        r2 = rule2_hits(text, forbidden_words)
        if r2:
            reasons.append("规则二：出现违禁词「%s」" % "、".join(r2))
    if enabled["r3"]:
        ok3, why = rule3_check(text, keywords, competitors)
        if not ok3:
            reasons.append(why)
    return {"passed": not reasons, "reasons": reasons}


def col_letter(n):
    """1 -> A, 2 -> B, ... 27 -> AA"""
    s = ""
    while n > 0:
        n, r = divmod(n - 1, 26)
        s = chr(65 + r) + s
    return s
