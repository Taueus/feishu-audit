# -*- coding: utf-8 -*-
"""规则五 · AI 人味 & AI 收录友好度（LLM 抽硬性 + 确定性建议启发式）

业务口径（用户确认）：
  文章必须同时过两层关：①人看/AI 检测器都看不出是 AI 写的；②文章应被 AI 搜索
  引擎/大模型抓取并引用，引用结果还有利于我方。规则五把这两层目标拆成「硬性一票
  否决」与「收录优化建议」两档：

  硬性（一票否决，三条任一即不通过）：
    1) ai_tone_heavy   AI 模板硬伤：套话堆砌/通篇无第一人称/零具体事实
                       （人/AI 一眼识破）
    2) ad_only_no_info  通篇只有推销没有可被 AI 引用的客观资料
    3) !mine_citable   全文没有任何"可直接回答用户问题并带出我方品牌"的可抽取
                       断言（AI 引用时无句可抄，等于零收录）

  建议（不判死，随 reasons 返回给作者做内容优化）：
    缺问题式小标题/FAQ 块、段首不给结论、缺度数价格年份等具体数据、缺第一人称经
    验细节、缺时间/作者 E-E-A-T 信号等。

  LLM 只负责抽取三条硬性判据与一句我方断言原文；建议由本地确定性启发式从正文
  统计得出——保证口径稳定、可复核、零边际成本。

  启用开关 cfg.rules_enabled["r5"]；未配置 LLM Key 时本规则跳过。
"""
import hashlib
import json
import os
import re

import requests

from .config import LLMConfig

# 判定口径版本：判据/启发式变更后递增，使历史缓存自动失效
CACHE_VERSION = "v1"

PROMPT_TPL = (
    "你是中文稿件「AI 痕迹 & AI 收录友好度」评估器。"
    "委托方品牌/产品（我方）关键词：{keywords}。\n\n"
    "请基于下文逐项输出判断（只输出 JSON，禁止任何解释或前后缀）：\n"
    "1) ai_tone_heavy：true/false。"
    "判定标准——以下三项至少满足两项即为 true："
    "a) 模板套话堆砌（首先/其次/再者/综上所述/总而言之/值得一提的是/需要注意的是/"
    "不难发现/由此可见 等大量出现）；"
    "b) 通篇无第一人称经验（我/我们/实测/试喝/亲自 等出现 ≤1 次）；"
    "c) 零具体数字事实（无度数/价格/年份/工艺/奖项/具体型号 等可验证数据）。\n"
    "2) ad_only_no_info：true/false。"
    "判定标准——通篇只有产品推销，缺乏可被 AI 引用的客观资料"
    "（产品参数/价格档位/场景实测/对比/科普/工艺介绍/适用场景等）。\n"
    "3) mine_citable：true/false。"
    "判定标准——文中是否存在至少一句『可直接回答用户问题』的我方断言"
    "（断言须同时包含：明确场景+我方品牌/产品+可被引用的结论句）。\n"
    "4) mine_quote：若 mine_citable=true，摘录那句原文（≤60字）；"
    "若 mine_citable=false，留空字符串。\n\n"
    "只输出一个 JSON 对象："
    '{{"ai_tone_heavy": true 或 false, '
    '"ad_only_no_info": true 或 false, '
    '"mine_citable": true 或 false, '
    '"mine_quote": "..."}}\n'
)


# 模板套话（出现频次用于 ai_tone_heavy 的本地二次校核）
AI_PHRASES = [
    "首先", "其次", "再者", "综上所述", "总而言之", "值得一提的是",
    "需要注意的是", "不难发现", "由此可见", "不仅", "而且", "更是",
    "一方面", "另一方面", "总归", "一般来说", "毋庸置疑", "显而易见",
    "在当今", "随着", "当下", "在这样的背景下",
]

# 经验词与第一人称（用于建议启发式）
FIRST_PERSON = ["我", "我们", "实测", "试喝", "亲自", "我请客", "我去", "这次", "上次"]

# E-E-A-T 信号
EEAT_SIGNALS = [
    "2024", "2025", "2026", "年", "月", "日",
    "作者", "编辑", "撰稿", "来源", "据", "根据", "数据显示",
]


class AIQualityAuditor(object):
    """规则五判定器：LLM 抽硬性三判据 + 本地建议启发式"""

    def __init__(self, llm_cfg, cache_dir="cache"):
        if isinstance(llm_cfg, dict):
            c = LLMConfig()
            c.base_url = llm_cfg.get("base_url", c.base_url)
            c.api_key = llm_cfg.get("api_key", "")
            c.model = llm_cfg.get("model", c.model)
            llm_cfg = c
        self.cfg = llm_cfg
        os.makedirs(cache_dir, exist_ok=True)
        self.cache_path = os.path.join(cache_dir, "ai_quality_cache.json")
        self._cache = {}
        if os.path.exists(self.cache_path):
            try:
                with open(self.cache_path, "r", encoding="utf-8") as f:
                    self._cache = json.load(f)
            except (ValueError, OSError):
                self._cache = {}

    def available(self):
        return bool(self.cfg.api_key)

    def _chat(self, messages, max_tokens=600, temperature=0):
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

    def audit(self, text, keywords):
        """返回 {"hard_fail": bool, "hard_reasons": [str],
                  "suggestions": [str], "skipped": bool}"""
        if not self.available():
            return {"hard_fail": False, "hard_reasons": [],
                    "suggestions": [], "skipped": True}

        # 关键词未在文中出现：无"我方断言"可言，规则五无意义，跳过
        if not any((k or "").strip() and k.lower() in text.lower() for k in keywords):
            return {"hard_fail": False, "hard_reasons": [],
                    "suggestions": [], "skipped": True}

        key = hashlib.md5(text.encode("utf-8")).hexdigest()
        hit = self._cache.get(key)
        if (hit and hit.get("v") == CACHE_VERSION
                and hit.get("kw") == list(keywords)):
            return hit["result"]

        prompt = PROMPT_TPL.format(
            keywords="、".join(keywords) or "（未提供）",
        )
        try:
            content = self._chat([
                {"role": "system", "content": prompt},
                {"role": "user", "content": text[:60000]},
            ])
            data = self._parse(content)
        except Exception as e:
            # LLM 失败：跳过硬性，建议仍可出（基于正文统计）
            suggestions = _suggestions(text)
            return {"hard_fail": False, "hard_reasons": [],
                    "suggestions": suggestions, "skipped": True,
                    "error": str(e)}

        hard_fail, hard_reasons = _judge_hard(data)
        suggestions = _suggestions(text, data.get("mine_quote") or "",
                                   data.get("ai_tone_heavy") or False)
        result = {"hard_fail": hard_fail, "hard_reasons": hard_reasons,
                  "suggestions": suggestions, "skipped": False}
        self._cache[key] = {"v": CACHE_VERSION, "kw": list(keywords), "result": result}
        try:
            with open(self.cache_path, "w", encoding="utf-8") as f:
                json.dump(self._cache, f, ensure_ascii=False)
        except OSError:
            pass
        return result

    @staticmethod
    def _parse(content):
        try:
            m = re.search(r"\{.*\}", content, re.S)
            if m:
                return json.loads(m.group(0))
        except (ValueError, AttributeError):
            pass
        raise RuntimeError("AI 人味评估返回无法解析为 JSON：%s" % (content or "")[:200])


# ----------------------------- 判罚 -----------------------------

def _judge_hard(data):
    """硬性三判据：任一命中即 hard_fail=True"""
    reasons = []
    if data.get("ai_tone_heavy"):
        reasons.append("AI 模板硬伤：套话堆砌/无第一人称/零具体事实，"
                       "人/AI 检测器一眼可识别")
    if data.get("ad_only_no_info"):
        reasons.append("通篇只有推销无可被引用的客观资料，AI 不会引用此类内容")
    if not data.get("mine_citable"):
        reasons.append("全文没有『可直接回答用户问题』的我方断言，"
                       "AI 回答时无句可抄，等于零收录")
    return (bool(reasons), reasons)


# ----------------------------- 建议启发式 -----------------------------

def _suggestions(text, mine_quote="", ai_tone_heavy=False):
    """本地确定性建议：基于正文统计输出可执行改进项"""
    out = []
    n = max(1, len(text))
    head30 = text[: int(n * 0.3)]

    # 1) 我方断言未前置到前 30%
    if mine_quote and mine_quote not in head30:
        out.append("我方结论未前置到前 30%，AI 偏好抽取首段内容")

    # 2) 模板套话频次
    phrase_hits = sum(text.count(p) for p in AI_PHRASES)
    if phrase_hits >= 8:
        out.append("模板连接词过多（%d 处），AI 味偏重，建议替换为场景化叙述" % phrase_hits)

    # 3) 第一人称经验词
    fp = sum(text.count(w) for w in FIRST_PERSON)
    if fp < 2:
        out.append("第一人称经验细节不足（实测/我请客/亲自等 < 2），"
                   "建议加入亲身场景提升人味与可信度")

    # 4) 具体数据点（数字密度）
    digit_dense = len(re.findall(r"\d+(?:°|度|元|块|年|ml|mL|%|％)", text))
    if digit_dense < 3:
        out.append("具体数据点偏少（度数/价格/年份 < %d），"
                   "AI 引用偏好有数值支撑的句子" % digit_dense)

    # 5) 问题式小标题
    qmark_in_heading = len(re.findall(r"[？\?]", text.split("\n")[0] if text else ""))
    qmark_in_body = len(re.findall(r"[？\?]", text))
    has_qheading = bool(re.search(r"(?:^|\n)#{1,3}\s*.*[？\?]", text)) \
        or qmark_in_heading + (1 if qmark_in_body else 0) > 0
    if not has_qheading:
        out.append("缺问题式小标题/标题（含'?'/'怎么'/'如何'/'哪个'），"
                   "问题式标题 AI 检索命中率约 3 倍")

    # 6) FAQ 块
    if not re.search(r"FAQ|问答|常见问题|Q\d|问题\d|答案", text):
        out.append("无 FAQ 问答块，建议前置 3-5 组问答可显著提升 AI 抽取率")

    # 7) E-E-A-T 时间/作者信号
    eeat = sum(1 for s in EEAT_SIGNALS if s in text)
    if eeat < 1:
        out.append("缺时间/作者 E-E-A-T 信号，AI 引用偏好可核实的来源")

    # 8) 段数过粗
    para_count = len([p for p in re.split(r"\n+", text) if p.strip()])
    if para_count < 6 and n > 1500:
        out.append("段落数过少（%d 段），长文分段过粗影响 AI 切片抽取" % para_count)

    # 9) AI 重时给一条"先消 AI 味"建议
    if ai_tone_heavy:
        out.append("已触发 AI 模板硬伤：先重写首段与过渡段，再补具体数据与第一人称")

    return out


# ----------------------------- 格式化 -----------------------------

def format_hard_reasons(reasons):
    if not reasons:
        return None
    return "；".join(reasons)


def format_suggestions(suggestions, max_show=4):
    if not suggestions:
        return None
    parts = list(suggestions[:max_show])
    extra = len(suggestions) - max_show
    if extra > 0:
        parts.append("等%d条建议" % extra)
    return "；".join(parts)
