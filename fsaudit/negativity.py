# -*- coding: utf-8 -*-
"""规则九 · 品牌负面描述判定（LLM 抽取事实 + 确定性代码判罚）

业务口径（2026-09-15 用户需求）：文章不能出现品牌（我方）和竞品的负面描述。
  - 我方被贬损 → 不通过（自砸招牌）
  - 竞品被贬损 → 不通过（涉嫌商业诋毁/不正当竞争风险）

分工：
  1) 本地快速路径（fsaudit/rules.py · rule9_local_hits）：强负面词
     （wordbooks/negative_terms.yaml，如「假货/翻车/避雷/智商税」）出现在品牌名
     前后 40 字内 → 直接判不通过，不依赖 LLM，LLM 故障也能兜底。
  2) 本模块（LLM 语义判定）：本地词库未命中时，识别不含强负面词的隐性负面描述
     ——如「XX用了三天就坏了」「网上骂声一片」等。LLM 只负责抽取客观事实
     （谁被负面描述、原文证据），**是否违规由 judge() 固化判罚：命中任一条即不通过**。

不算负面描述（提示词中明确排除，防误伤）：
  - 客观对比优势（「我方在某方面优于竞品」）；
  - 未指向具体品牌的泛泛吐槽；
  - 无贬损色彩的客观事实陈述。

判定结果按 md5(正文)+关键词/竞品清单 本地缓存（negativity_cache.json），
重复审不再调模型；未配置 LLM Key 或调用失败时本规则跳过（不判不通过）。
"""
import hashlib
import json
import os
import re
import threading

import requests

from .config import LLMConfig

# 判定口径版本：判据/提示词变更后递增，使历史缓存自动失效
CACHE_VERSION = "v1"

PROMPT_TPL = (
    "你是中文稿件「品牌负面描述审查器」。我方品牌/产品关键词：{keywords}。"
    "参考竞品清单：{competitors}。审查范围仅限这两类品牌（含其子品牌/产品/系列名变体）。\n\n"
    "任务：找出正文中针对上述任何具体品牌的负面描述，包括但不限于：\n"
    "- 贬损性评价（质量差/不行/难用/失望/后悔/别买）；\n"
    "- 负面口碑（差评/投诉/维权/退货/骂声一片）；\n"
    "- 负面事件（丑闻/造假/被曝光/安全事故/翻车）；\n"
    "- 避雷/踩雷/劝退类且指向具体品牌的表述。\n\n"
    "以下情况【不算】负面描述，严禁输出：\n"
    "- 客观对比（如「我方在续航上优于竞品X」）；\n"
    "- 未指向具体品牌的泛泛吐槽（如「市面上不少产品都虚标」）；\n"
    "- 无贬损色彩的客观陈述（如「竞品X成立于2010年」）。\n\n"
    "只输出一个 JSON 对象，禁止任何解释或前后缀：\n"
    '{{"negatives": [{{"target": "被负面描述的品牌名", "side": "mine或competitor", '
    '"evidence": "原文摘录（30字内）", "why": "为何属于负面描述（10字内）"}}]}}\n'
    "没有负面描述时输出 {{\"negatives\": []}}。"
)


class NegativityAuditor(object):
    """规则九判定器：LLM 抽取负面描述事实，judge() 按固化口径判罚"""

    def __init__(self, llm_cfg, cache_dir="cache"):
        if isinstance(llm_cfg, dict):
            c = LLMConfig()
            c.base_url = llm_cfg.get("base_url", c.base_url)
            c.api_key = llm_cfg.get("api_key", "")
            c.model = llm_cfg.get("model", c.model)
            llm_cfg = c
        self.cfg = llm_cfg
        os.makedirs(cache_dir, exist_ok=True)
        self.cache_path = os.path.join(cache_dir, "negativity_cache.json")
        self._cache = {}
        self._lock = threading.Lock()   # 并发审核时保护缓存读写
        if os.path.exists(self.cache_path):
            try:
                with open(self.cache_path, "r", encoding="utf-8") as f:
                    self._cache = json.load(f)
            except (ValueError, OSError):
                self._cache = {}

    def available(self):
        return bool(self.cfg.api_key)

    def _chat(self, messages, max_tokens=1200, temperature=0):
        r = requests.post(
            self.cfg.base_url.rstrip("/") + "/chat/completions",
            headers={"Authorization": "Bearer " + self.cfg.api_key,
                     "Content-Type": "application/json"},
            json={"model": self.cfg.model, "temperature": temperature,
                  "max_tokens": max_tokens, "messages": messages},
            timeout=40,
        )
        if r.status_code != 200:
            raise RuntimeError("LLM HTTP %d：%s" % (r.status_code, r.text[:200]))
        return r.json()["choices"][0]["message"]["content"]

    def audit(self, text, keywords, competitors):
        """返回 {"pass": bool, "issues": [...]}；未配置 key 时按跳过处理"""
        if not self.available():
            return {"pass": True, "issues": [], "skipped": True}
        kw, comps = list(keywords or []), list(competitors or [])
        key = hashlib.md5(text.encode("utf-8")).hexdigest()
        with self._lock:
            hit = self._cache.get(key)
            if (hit and hit.get("v") == CACHE_VERSION
                    and hit.get("kw") == kw and hit.get("comps") == comps):
                return {"pass": not hit["issues"], "issues": hit["issues"]}
        prompt = PROMPT_TPL.format(
            keywords="、".join(kw) or "（未提供）",
            competitors="、".join(comps[:60]) or "（无）",
        )
        content = self._chat([
            {"role": "system", "content": prompt},
            {"role": "user", "content": text[:60000]},
        ])
        data = self._parse(content)
        issues = judge(data.get("negatives") or [], kw, comps)
        with self._lock:
            self._cache[key] = {"v": CACHE_VERSION, "kw": kw, "comps": comps,
                                "issues": issues}
            try:
                with open(self.cache_path, "w", encoding="utf-8") as f:
                    json.dump(self._cache, f, ensure_ascii=False)
            except OSError:
                pass
        return {"pass": not issues, "issues": issues}

    @staticmethod
    def _parse(content):
        try:
            m = re.search(r"\{.*\}", content, re.S)
            if m:
                return json.loads(m.group(0))
        except (ValueError, AttributeError):
            pass
        raise RuntimeError("负面描述解析返回无法解析为JSON：%s" % (content or "")[:200])


def judge(negatives, keywords, competitors):
    """确定性判罚：命中任一针对我方或竞品的负面描述即不通过。
    target 只做展示用途（LLM 输出），不作为放行依据——LLM 抽到负面条目即判。"""
    issues = []
    for n in negatives:
        if not isinstance(n, dict):
            continue
        target = (n.get("target") or "").strip()
        evidence = (n.get("evidence") or "").strip()
        why = (n.get("why") or "").strip()
        side = (n.get("side") or "").strip().lower()
        if not target and not evidence:
            continue
        issues.append({
            "target": target or "未具名品牌",
            "side": "mine" if side == "mine" else "competitor",
            "evidence": evidence,
            "why": why,
        })
    return issues


def format_negatives(issues, max_show=3):
    """把 issues 拼成可读原因（用于不通过原因列与日志）"""
    if not issues:
        return None
    parts = []
    for it in issues[:max_show]:
        side = "我方" if it.get("side") == "mine" else "竞品"
        seg = "%s品牌「%s」出现负面描述" % (side, it.get("target", "?"))
        ev = (it.get("evidence") or "").strip()
        why = (it.get("why") or "").strip()
        if ev:
            seg += "（%s）" % ev
        elif why:
            seg += "（%s）" % why
        parts.append(seg)
    extra = len(issues) - max_show
    if extra > 0:
        parts.append("等%d处问题" % extra)
    return "；".join(parts)
