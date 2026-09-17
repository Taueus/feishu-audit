# -*- coding: utf-8 -*-
"""合并 LLM 调用（一次请求同时完成品牌识别 + 规则四 + 规则五 + 规则九抽取）

降本背景（2026-09-17 诊断）：原先一篇文章要走 4 个独立 LLM 模块
（llm.py 品牌识别 / viewpoint.py 规则四 / ai_quality.py 规则五 /
negativity.py 规则九），每个模块都把全文 text[:60000] 发一遍——
每篇 4 次请求、全文重复发送 4 次，160 篇就烧掉 193 万 tokens。

本模块把四个模块的「LLM 抽取任务」合并为一次请求：
  - LLM 只负责抽取客观事实（品牌列表 / 观点块属性 / 规则五三判据 /
    负面描述条目），一次返回；
  - **判罚仍由各规则的确定性 judge() 固化执行**（复用 viewpoint.judge /
    ai_quality._judge_hard / negativity.judge），口径与原先完全一致；
  - 结果按 md5(正文)+关键词+请求任务集 缓存（combined_cache.json），
    重复审不再调模型；
  - 未请求的任务（规则开关关闭）不出现在 prompt 与输出中，省输出 tokens；
  - 未配置 LLM Key 时返回 None（上层退回本地词库模式）。

故障降级（engine 侧）：合并调用失败时退回「本地词库 + 各规则跳过」，
不会再拆成 4 次单独调用补发（避免故障时反而放大消耗）；如需恢复
旧链路，config.yaml 设 llm_combined: false。
"""
import hashlib
import json
import os
import re
import threading

import requests

from .config import LLMConfig

# 判定口径版本：prompt/输出结构变更后递增，使历史缓存自动失效
CACHE_VERSION = "v1"

# 各任务段落（与原四模块 prompt 口径逐字对齐，只做合并不改判据）
_TPL_BRANDS = (
    "【任务A·品牌实体识别】\n"
    "提取正文中所有出现的品牌名、产品名、公司名（包括我方品牌和竞品品牌，含子品牌/"
    "系列名变体），输出到 \"brands\" 字段（字符串数组，如 [\"品牌A\",\"品牌B\"]）。"
    "正文中出现的其他同类品牌一律视为竞品，一并提取。没有品牌时输出 []。\n"
)

_TPL_R4 = (
    "【任务B·观点块解析】\n"
    "把正文切成「观点/小节/场景/价位档」等有立场的块（如：商务宴请推荐、喜庆宴席推荐、"
    "亲友小聚推荐、XX价位推荐、避雷提醒、场景攻略、浓香阵营盘点、XX价位横评、榜单等）；"
    "纯科普、无品牌推荐倾向的铺垫/引子段不算观点块，不要切出。对每一块如实填写下列字段，"
    "不要做违规判断、不要发表审核意见，只客观抽取，输出到 \"blocks\" 数组：\n"
    "- name：块名或一句话摘要；\n"
    "- kind：negative=避雷/别买/不推荐/翻车/劝退/差评类负面块；reco=带明确推荐倾向的块（出现"
    "'推荐/优先选/适合选/可以选/首选'等字样）；list=产品盘点/阵营横评/榜单归位/多款并列介绍块"
    "（通常只做客观介绍、无'推荐XX'字样）；info=上述都不好归类但有品牌立场时用；\n"
    "- mine_present：块内是否出现我方品牌/产品（含子品牌/系列名）；\n"
    "- mine_lead：仅当 mine_present 时有效——我方是否该块主角，需同时满足：①我方是块内被首个"
    "点名介绍/推荐的（或被排在推荐首位）；②我方在该块内的正面展开不少于块内任何单一竞品；"
    "③没有竞品被'其中/尤其/首推'等单独拎出来夸赞而压过我方。任一条不满足即为 false；\n"
    "- comps_count：块内被介绍或推荐到的竞品品牌数（去重计数；只算有实质介绍/推荐笔墨的，"
    "一笔带过凑数的可不算）；\n"
    "- evidence：一句原文证据摘录（引用原文关键词，说明块内点名顺序/谁被重点展开/有无推荐字样），"
    "供复核。\n"
    "拆分要求：同一节内并列的多个推荐条目必须各自单独成块——如（1）场景一…（2）场景二…、"
    "'①…②…'、或带小标题的每个小节，均不得互相合并成一个大块；仅当整节只有一个推荐立场时才"
    "可作为一块。务必逐块输出，不要合并或遗漏观点块。\n"
)

_TPL_R5 = (
    "【任务C·AI 痕迹 & AI 收录友好度】\n"
    "基于正文逐项输出判断到 \"ai_quality\" 对象（只做事实判断）：\n"
    "1) ai_tone_heavy：true/false。判定标准——以下三项至少满足两项即为 true："
    "a) 模板套话堆砌（首先/其次/再者/综上所述/总而言之/值得一提的是/需要注意的是/"
    "不难发现/由此可见 等大量出现）；"
    "b) 通篇无第一人称经验（我/我们/实测/试喝/亲自 等出现 ≤1 次）；"
    "c) 零具体数字事实（无度数/价格/年份/工艺/奖项/具体型号 等可验证数据）。\n"
    "2) ad_only_no_info：true/false。判定标准——通篇只有产品推销，缺乏可被 AI 引用的客观资料"
    "（产品参数/价格档位/场景实测/对比/科普/工艺介绍/适用场景等）。\n"
    "3) mine_citable：true/false。判定标准——文中是否存在至少一句『可直接回答用户问题』的我方断言"
    "（断言须同时包含：明确场景+我方品牌/产品+可被引用的结论句）。\n"
    "4) mine_quote：若 mine_citable=true，摘录那句原文（≤60字）；若 mine_citable=false，留空字符串。\n"
)

_TPL_R9 = (
    "【任务D·品牌负面描述】\n"
    "找出正文中针对我方或任何具体竞品品牌的负面描述（同类品牌均视为竞品），包括但不限于：\n"
    "- 贬损性评价（质量差/不行/难用/失望/后悔/别买）；\n"
    "- 负面口碑（差评/投诉/维权/退货/骂声一片）；\n"
    "- 负面事件（丑闻/造假/被曝光/安全事故/翻车）；\n"
    "- 避雷/踩雷/劝退类且指向具体品牌的表述。\n"
    "以下情况【不算】负面描述，严禁输出：\n"
    "- 客观对比（如「我方在续航上优于竞品X」）；\n"
    "- 未指向具体品牌的泛泛吐槽（如「市面上不少产品都虚标」）；\n"
    "- 无贬损色彩的客观陈述（如「竞品X成立于2010年」）。\n"
    "输出到 \"negatives\" 数组，每条：\n"
    '{{"target": "被负面描述的品牌名", "side": "mine或competitor", '
    '"evidence": "原文摘录（30字内）", "why": "为何属于负面描述（10字内）"}}\n'
    "没有负面描述时输出 []。\n"
)


class CombinedAuditor(object):
    """合并调用判定器：一次 LLM 请求完成四项抽取，判罚由确定性代码执行"""

    def __init__(self, llm_cfg, cache_dir="cache"):
        if isinstance(llm_cfg, dict):
            c = LLMConfig()
            c.base_url = llm_cfg.get("base_url", c.base_url)
            c.api_key = llm_cfg.get("api_key", "")
            c.model = llm_cfg.get("model", c.model)
            llm_cfg = c
        self.cfg = llm_cfg
        os.makedirs(cache_dir, exist_ok=True)
        self.cache_path = os.path.join(cache_dir, "combined_cache.json")
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

    def _chat(self, messages, max_tokens=3500, temperature=0):
        r = requests.post(
            self.cfg.base_url.rstrip("/") + "/chat/completions",
            headers={"Authorization": "Bearer " + self.cfg.api_key,
                     "Content-Type": "application/json"},
            json={"model": self.cfg.model, "temperature": temperature,
                  "max_tokens": max_tokens, "messages": messages},
            timeout=60,
        )
        if r.status_code != 200:
            raise RuntimeError("LLM HTTP %d：%s" % (r.status_code, r.text[:200]))
        return r.json()["choices"][0]["message"]["content"]

    def audit(self, text, keywords, want=None):
        """一次请求返回全部抽取结果。

        want: dict，可含 "brands"/"r4"/"r5"/"r9"，True 才请求对应任务
              （缺省全部请求；brands 默认必请求——本地规则三/八需要竞品清单）。
        返回 {"brands": [...], "blocks": [...], "ai_quality": {...},
              "negatives": [...]}；未配置 key 时返回 None。
        """
        if not self.available():
            return None
        kw = list(keywords or [])
        w = {"brands": True, "r4": True, "r5": True, "r9": True}
        if want:
            for k in w:
                if k in want:
                    w[k] = bool(want[k])
            w["brands"] = w["brands"] or True   # brands 恒为 True（本地规则依赖）
        key = hashlib.md5(text.encode("utf-8")).hexdigest()
        with self._lock:
            hit = self._cache.get(key)
            if (hit and hit.get("v") == CACHE_VERSION
                    and hit.get("kw") == kw and hit.get("want") == w):
                return hit["data"]

        prompt = self._build_prompt(kw, w)
        content = self._chat([
            {"role": "system", "content": prompt},
            {"role": "user", "content": text[:60000]},
        ])
        data = self._parse(content, w)
        with self._lock:
            self._cache[key] = {"v": CACHE_VERSION, "kw": kw, "want": w,
                                "data": data}
            try:
                with open(self.cache_path, "w", encoding="utf-8") as f:
                    json.dump(self._cache, f, ensure_ascii=False)
            except OSError:
                pass
        return data

    @staticmethod
    def _build_prompt(keywords, w):
        parts = [
            "你是中文稿件「综合审查器」。委托方品牌/产品（我方）关键词：{kw}。"
            "正文中出现的其他同类品牌一律视为竞品。"
            "请对用户给出的文章一次性完成下列抽取任务，只输出一个 JSON 对象，"
            "禁止任何解释、前后缀或其他内容。\n".format(
                kw="、".join(keywords) or "（未提供）"),
        ]
        fields = []
        if w["brands"]:
            parts.append(_TPL_BRANDS)
            fields.append('"brands": ["品牌A", ...]')
        if w["r4"]:
            parts.append(_TPL_R4)
            fields.append('"blocks": [{"name": "...", "kind": "negative|reco|list|info", '
                          '"mine_present": true, "mine_lead": false, '
                          '"comps_count": 0, "evidence": "..."}]')
        if w["r5"]:
            parts.append(_TPL_R5)
            fields.append('"ai_quality": {"ai_tone_heavy": false, '
                          '"ad_only_no_info": false, "mine_citable": true, '
                          '"mine_quote": "..."}')
        if w["r9"]:
            parts.append(_TPL_R9)
            fields.append('"negatives": [{"target": "...", "side": "mine", '
                          '"evidence": "...", "why": "..."}]')
        parts.append(
            "只输出形如下列结构的 JSON 对象（未请求的任务不出现对应字段或输出空值）：\n"
            "{%s}" % ", ".join(fields))
        return "\n".join(parts)

    @staticmethod
    def _parse(content, w):
        try:
            m = re.search(r"\{.*\}", content, re.S)
            if not m:
                raise ValueError("no json")
            data = json.loads(m.group(0))
        except (ValueError, AttributeError):
            raise RuntimeError("综合审查返回无法解析为JSON：%s" % (content or "")[:200])
        if not isinstance(data, dict):
            raise RuntimeError("综合审查返回不是JSON对象：%s" % (content or "")[:200])
        # 规范化：未请求/缺失字段给安全空值，判罚侧无需再判空
        brands = data.get("brands")
        if not isinstance(brands, list):
            brands = []
        blocks = data.get("blocks")
        if not isinstance(blocks, list):
            blocks = []
        aq = data.get("ai_quality")
        if not isinstance(aq, dict):
            aq = {}
        negs = data.get("negatives")
        if not isinstance(negs, list):
            negs = []
        return {"brands": [str(b).strip() for b in brands if str(b).strip()],
                "blocks": blocks, "ai_quality": aq, "negatives": negs}

    def selfcheck(self):
        """只验证连通性：能返回 200 并有内容即通过"""
        self._chat([{"role": "user", "content": "回复OK"}], max_tokens=16)
