# -*- coding: utf-8 -*-
"""审核规则引擎

规则一：AI 痕迹词库匹配（wordbooks/ai_terms.yaml）
规则二：写死违禁词精确匹配（默认「AI生成」「免责声明」）
规则三：我方关键词首次出现位置必须早于任何竞品
规则六：广告法绝对化用语（wordbooks/absolute_terms.yaml，子串+正则）
规则七：FAQ 问答结构（FAQ 段落须为英文 Q/A 问答，中文「问/答」判不通过）
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


def load_my_products():
    """自有品牌/产品白名单（brands.yaml 的 my_products 段）：
    识别出的实体命中白名单时不计入竞品（如本品牌旗下产品、子品牌、系列名）。"""
    return _read_yaml("brands.yaml", "my_products")


def load_absolute_terms():
    """加载广告法绝对化用语词库，返回 (terms, patterns)。
    词库文件缺失或某一段为空时对应返回空列表。"""
    path = os.path.join(WORDBOOK_DIR, "absolute_terms.yaml")
    terms, patterns = [], []
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        terms = [str(x).strip() for x in (data.get("terms") or []) if str(x).strip()]
        patterns = [str(x).strip() for x in (data.get("patterns") or []) if str(x).strip()]
    return terms, patterns


def split_keywords(cell):
    """把「项目」单元格拆成多个关键词"""
    cell = (cell or "").strip()
    if not cell:
        return []
    parts = [p for p in re.split(r"[,，、;；/|｜\s]+", cell) if p]
    return parts or [cell]


def _squash(s):
    """去掉空格/制表符/全角空格（保留换行），用于空白不敏感匹配。

    现实案例：CSDN 平台水印「（注：部分内容可能由 AI 生成）」在 AI 与生成
    之间夹了空格，普通子串匹配会漏判；压掉空白后即可命中「AI生成」。"""
    return re.sub(r"[ \t\u3000]+", "", s or "")


def rule1_hits(text, terms):
    t = _squash(text).lower()
    return [term for term in terms if _squash(term).lower() in t]


def rule2_hits(text, words):
    t = _squash(text).lower()
    return [w for w in words if _squash(w).lower() in t]


# 规则六命中展示上限（防止极端文章刷屏）
ABSOLUTE_DISPLAY_CAP = 10


def rule6_hits(text, terms, patterns):
    """广告法绝对化用语命中检测。
    terms 走子串匹配（不区分大小写），patterns 走正则匹配；
    命中去重 + 子串折叠：某命中被另一命中完整包含时只报较长者
    （如命中「最顶级」时不再重复报「顶级」）。"""
    t = text or ""
    low = _squash(t).lower()
    hits = set()
    for w in terms:
        if _squash(w).lower() in low:
            hits.add(w)
    for p in patterns:
        try:
            for m in re.finditer(p, t):
                s = m.group(0).strip()
                if s:
                    hits.add(s)
        except re.error:
            continue  # 词库里的非法正则直接跳过，不炸审核
    out = [h for h in hits if not any(h != k and h in k for k in hits)]
    out.sort(key=len, reverse=True)
    return out


# 关键词/品牌名 token 拆分：拉丁字母数字段（≥2位）或中文连续段（≥2字）。
# 用于缩写回退匹配，如「科洛百KLB」拆出 [科洛百, KLB]，文中只写 KLB 也算命中。
_TOKEN_RE = re.compile(r"[A-Za-z0-9]{2,}|[\u4e00-\u9fff]{2,}")


def keyword_tokens(name):
    """把关键词/品牌名拆成 token（中文连续段 + 拉丁字母数字段）"""
    return _TOKEN_RE.findall(name or "")


def _tok_related(a, b):
    """两个 token 是否相关：相等或互为子串（≥2位才有意义）"""
    if a == b:
        return True
    if len(a) >= 2 and a in b:
        return True
    if len(b) >= 2 and b in a:
        return True
    return False


def find_keyword(text, kw):
    """返回关键词在文本中首次出现的位置（找不到返回 -1），不区分大小写。
    先整词匹配；整词未命中时按 token 回退——关键词里的任一 token
    （如「科洛百KLB」的 KLB）出现在文中即视为我方关键词出现。
    审核表口径：关键词出现缩写/子串即算，不要求完整品牌全称。"""
    t = (text or "").lower()
    k = (kw or "").strip().lower()
    if not k:
        return -1
    i = t.find(k)
    if i >= 0:
        return i
    for tok in keyword_tokens(k):
        j = t.find(tok.lower())
        if j >= 0:
            return j
    return -1


def keyword_in_text(text, kw):
    """关键词是否出现在文中（整词或 token 级）"""
    return find_keyword(text, kw) >= 0


def is_mine(brand, keywords):
    """判断识别出的品牌/产品是否属于我方：
    与任一关键词存在子串关系，或 token 级相关
    （如关键词「科洛百KLB」与品牌「KLB时光棒精华」共享 KLB）。"""
    b = (brand or "").strip()
    if not b:
        return False
    btoks = keyword_tokens(b)
    for k in keywords:
        kk = (k or "").strip()
        if not kk:
            continue
        if b in kk or kk in b:
            return True
        if any(_tok_related(x, y) for x in btoks for y in keyword_tokens(kk)):
            return True
    return False


def is_own_product(name, my_products):
    """品牌/产品名是否命中自有产品白名单（子串或 token 级相关）"""
    n = (name or "").strip()
    if not n:
        return False
    ntoks = keyword_tokens(n)
    for p in my_products:
        pp = (p or "").strip()
        if not pp:
            continue
        if pp in n or n in pp:
            return True
        if any(_tok_related(x, y) for x in ntoks for y in keyword_tokens(pp)):
            return True
    return False


# ---------------- 规则七 · FAQ 问答结构 ----------------
# FAQ 段落标题标记（行内含且行长视为标题，如「常见问题解答」「FAQ」「四、常见问题」）
FAQ_HEADING = re.compile(r"(?i)(FAQ|Q\s*&\s*A|常见问题|常见问答|问题解答|答疑)")
# 英文问答项标记：行首或句末标点后的 Q/A + 可选编号 + 分隔符（Q1：/Q:/A2.）或直接接中文
_Q_MARK = re.compile(r"(?im)(?:^|[\n。！？!?])\s*[Qq]\s*\d*\s*(?:[：:．.、)）]|[^\x00-\x7f])")
_A_MARK = re.compile(r"(?im)(?:^|[\n。！？!?])\s*[Aa]\s*\d*\s*(?:[：:．.、)）]|[^\x00-\x7f])")
# 中文问答项标记（规则七要求避免的形式；允许「问/问题/回答」后带编号，如「问题一：」「回答2:」）
_CN_Q_MARK = re.compile(r"(?:^|[\n。！？!?])\s*(?:问|提问|问题)\s*[0-9一二三四五六七八九十百]*\s*[：:]")
_CN_A_MARK = re.compile(r"(?:^|[\n。！？!?])\s*(?:答|回答|解答)\s*[0-9一二三四五六七八九十百]*\s*[：:]")
# 视为标题行的最大长度（超过则当作正文提及，不进入 FAQ 判定）
FAQ_HEADING_MAX_LEN = 20


def rule7_faq_check(text):
    """FAQ 问答结构检查，返回 (通过?, 原因)。

    文章无 FAQ 段落 → 规则不适用，直接通过；
    有 FAQ 段落时要求：
      1) 使用英文 Q/A 问答结构（Q1：… A1：… / Q:… A:… 等变体均可）
      2) 不得使用中文「问：/答：/问题：/回答：」形式
      3) Q 与 A 必须成对出现（有 Q 无 A 或有 A 无 Q 均不通过）
    """
    t = text or ""
    if not t:
        return True, None
    lines = t.split("\n")
    start = None
    for i, ln in enumerate(lines):
        s = ln.strip()
        # 短行且含 FAQ 标记 → 视为 FAQ 段落标题；正文中提到「常见问题」不算
        if len(s) <= FAQ_HEADING_MAX_LEN and FAQ_HEADING.search(s):
            start = i
            break
    if start is None:
        return True, None  # 无 FAQ 段落
    region = "\n".join(lines[start:])
    cn_q = len(_CN_Q_MARK.findall(region))
    cn_a = len(_CN_A_MARK.findall(region))
    if cn_q or cn_a:
        return False, ("规则七：FAQ 段落使用了中文「问/答」形式（问×%d、答×%d），"
                       "须改为英文 Q/A 问答结构" % (cn_q, cn_a))
    qn = len(_Q_MARK.findall(region))
    an = len(_A_MARK.findall(region))
    if qn == 0 and an == 0:
        return False, "规则七：检测到 FAQ 段落，但未形成问答结构（未发现 Q/A 标记）"
    if qn > 0 and an == 0:
        return False, "规则七：FAQ 段落有 Q 无 A，问答结构不完整"
    if an > 0 and qn == 0:
        return False, "规则七：FAQ 段落有 A 无 Q，问答结构不完整"
    return True, None


def rule3_check(text, keywords, competitors):
    """返回 (通过?, 原因)。
    我方关键词支持缩写/token 匹配：如关键词「科洛百KLB」，
    文中出现「KLB」或「科洛百」任一即算我方关键词出现。"""
    if not keywords:
        return False, "规则三：「项目」字段未填写关键词"
    idx = [find_keyword(text, k) for k in keywords]
    found = [i for i in idx if i >= 0]
    if not found:
        return False, "规则三：我方关键词（%s）未在文中出现" % "、".join(keywords)
    my_first = min(found)
    t = (text or "").lower()
    earlier = [b for b in competitors if 0 <= t.find(b.lower()) < my_first]
    if earlier:
        return False, "规则三：竞品「%s」首次出现早于我方关键词" % "、".join(earlier)
    return True, None


def audit_text(text, keywords, ai_terms, forbidden_words, competitors,
               rules_enabled=None, absolute=None):
    """执行确定性审核规则（规则一/二/三/六/七），返回 {"passed": bool, "reasons": [str]}
    rules_enabled: dict，键为 r1/r2/r3/r6/r7；缺省视为全开。
    absolute: (terms, patterns) 元组，规则六的绝对化用语词库；None 时跳过规则六。
    规则四/五（LLM 判定）由 engine 另行调用并按 r4/r5 开关决定是否触发。"""
    enabled = {"r1": True, "r2": True, "r3": True, "r6": True, "r7": True}
    if rules_enabled:
        for k in ("r1", "r2", "r3", "r6", "r7"):
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
    if enabled["r6"] and absolute:
        abs_terms, abs_patterns = absolute
        r6 = rule6_hits(text, abs_terms, abs_patterns)
        if r6:
            shown = "、".join(r6[:ABSOLUTE_DISPLAY_CAP])
            if len(r6) > ABSOLUTE_DISPLAY_CAP:
                shown += " 等共%d处" % len(r6)
            reasons.append("规则六：出现广告法绝对化用语「%s」" % shown)
    if enabled["r7"]:
        ok7, why7 = rule7_faq_check(text)
        if not ok7:
            reasons.append(why7)
    return {"passed": not reasons, "reasons": reasons}


def col_letter(n):
    """1 -> A, 2 -> B, ... 27 -> AA"""
    s = ""
    while n > 0:
        n, r = divmod(n - 1, 26)
        s = chr(65 + r) + s
    return s
