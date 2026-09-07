# -*- coding: utf-8 -*-
"""规则四 · 观点级主角性判定（LLM 抽取事实 + 确定性代码判罚）

业务口径（用户确认，多轮收敛）：
  稿子允许且需要竞品对比，但必须突出我方。审核**不按全文竞品/我方提及总量比对**，
  而是把正文切成「观点/小节/场景/价位档」逐块判定。LLM 只负责切块并抽取每块的
  客观属性（类型、是否含我方、竞品数量、原文证据），**是否违规由下方确定性代码
  judge() 判罚**，保证口径稳定、可复核。

判据（代码固化）：
  1) occupied           某观点块整块无我方，却成片推荐/展开竞品（竞品≥2款）；
                        单款竞品的场景补充、纯客观榜单/盘点归位不判。
  2) mine_in_negative   避雷/别买/不推荐/劝退/差评类负面块中出现我方。

【历史】原「competitor_leads · 竞品先于我方并占优」已合并到规则三的全文级位置比对，
由确定性代码判定，不再消耗 LLM 调用；规则四仅负责观点块层面的主场与负面块。

豁免：产品盘点/阵营横评/榜单类块（kind=list）中，客观罗列/归位一律不判（无
"推荐XX"立场时不构成观点压制）；真正带推荐倾向的块归 reco 走 occupied 判罚。

判定结果按 md5(正文) 本地缓存（viewpoint_cache.json），重复审不再调模型；
未配置 LLM API Key 或调用失败时本规则跳过（不判不通过），由上层决定降级策略。
"""
import hashlib
import json
import os
import re

import requests

from .config import LLMConfig

# 判定口径版本：判据/豁免变更后递增，使历史缓存自动失效（无需手动删除）
CACHE_VERSION = "v8"

PROMPT_TPL = (
    "你是中文稿件「观点块解析器」。委托方品牌/产品（我方）关键词：{keywords}。"
    "参考竞品清单：{competitors}（不限于此，正文里出现的其他酒类/品牌也一律视为竞品）。\n\n"
    "任务：把正文切成「观点/小节/场景/价位档」等有立场的块（如：商务宴请推荐、喜庆宴席推荐、"
    "亲友小聚推荐、XX价位推荐、避雷提醒、场景攻略、浓香阵营盘点、XX价位横评、榜单等）；"
    "纯科普、无品牌推荐倾向的铺垫/引子段不算观点块，不要切出。对每一块如实填写下列字段，"
    "不要做违规判断、不要发表审核意见，只客观抽取：\n"
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
    "供复核。\n\n"
    "只输出一个 JSON 对象，禁止任何解释或前后缀：\n"
    '{{"blocks": [{{"name": "...", "kind": "negative|reco|list|info", '
    '"mine_present": true 或 false, "mine_lead": true 或 false, '
    '"comps_count": 整数, "evidence": "..."}}]}}\n'
    "拆分要求：同一节内并列的多个推荐条目必须各自单独成块——如（1）场景一…（2）场景二…、"
    "'①…②…'、或带小标题的每个小节，均不得互相合并成一个大块；仅当整节只有一个推荐立场时才"
    "可作为一块。\n"
    "务必逐块输出，不要合并或遗漏观点块。"
)


class ViewpointAuditor(object):
    """规则四判定器：LLM 抽取观点块属性，judge() 按固化口径判罚"""

    def __init__(self, llm_cfg, cache_dir="cache"):
        if isinstance(llm_cfg, dict):
            c = LLMConfig()
            c.base_url = llm_cfg.get("base_url", c.base_url)
            c.api_key = llm_cfg.get("api_key", "")
            c.model = llm_cfg.get("model", c.model)
            llm_cfg = c
        self.cfg = llm_cfg
        os.makedirs(cache_dir, exist_ok=True)
        self.cache_path = os.path.join(cache_dir, "viewpoint_cache.json")
        self._cache = {}
        if os.path.exists(self.cache_path):
            try:
                with open(self.cache_path, "r", encoding="utf-8") as f:
                    self._cache = json.load(f)
            except (ValueError, OSError):
                self._cache = {}

    def available(self):
        return bool(self.cfg.api_key)

    def _chat(self, messages, max_tokens=2500, temperature=0):
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
        """返回 {"pass": bool, "issues": [...]}；未配置 key 或解析失败按跳过处理"""
        if not self.available():
            return {"pass": True, "issues": [], "skipped": True}
        key = hashlib.md5(text.encode("utf-8")).hexdigest()
        hit = self._cache.get(key)
        if (hit and hit.get("v") == CACHE_VERSION
                and hit.get("kw") == list(keywords)):
            return {"pass": not hit["issues"], "issues": hit["issues"]}
        prompt = PROMPT_TPL.format(
            keywords="、".join(keywords) or "（未提供）",
            competitors="、".join(competitors[:60]) or "（无）",
        )
        content = self._chat([
            {"role": "system", "content": prompt},
            {"role": "user", "content": text[:60000]},
        ])
        data = self._parse(content)
        issues = judge(data.get("blocks") or [], list(keywords))
        self._cache[key] = {"v": CACHE_VERSION, "kw": list(keywords),
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
        raise RuntimeError("观点块解析返回无法解析为JSON：%s" % (content or "")[:200])


def judge(blocks, keywords):
    """确定性判罚：把用户口径固化为代码。输入 LLM 抽取的 blocks，输出 issues 列表"""
    issues = []
    for b in blocks:
        if not isinstance(b, dict):
            continue
        name = (b.get("name") or "?").strip()
        kind = (b.get("kind") or "").strip().lower()
        mine = bool(b.get("mine_present"))
        lead = bool(b.get("mine_lead"))
        try:
            cc = int(b.get("comps_count") or 0)
        except (TypeError, ValueError):
            cc = 0
        ev = (b.get("evidence") or "").strip()

        if kind == "negative":
            if mine:
                issues.append({
                    "viewpoint": name, "type": "mine_in_negative",
                    "detail": "负面/避雷块中出现我方品牌或产品。%s" % ev,
                })
        elif kind == "list":
            # 盘点/横评/榜单归位类块一律不判：此类块为客观罗列/归位（无"推荐XX"的立场）。
            # 我方是主角→天然合规；我方仅被提及、竞品罗列展开→客观参考不构成观点压制；
            # 整块无我方→客观归位不判。真正带推荐倾向的块归 reco，不会漏。
            pass
        elif kind == "reco":
            # 观点块内我方也出现的"竞品先于我方并占优"情形已合并到规则三（确定性）
            # 规则四仅处理"整块无我方、竞品主场"与"我方进负面块"两类观点级违规
            if not mine and cc >= 2:
                issues.append({
                    "viewpoint": name, "type": "occupied",
                    "detail": "整块没有我方品牌，却成片推荐%d款竞品，该观点成为竞品主场。%s"
                              % (cc, ev),
                })
            # cc<2：单款竞品的场景补充，按口径不判
        # kind=info 或其它：不判（保守）
    return issues


TYPE_LABEL = {
    "occupied": "观点「%s」被竞品占据、我方非主角",
    "mine_in_negative": "负面/避雷场景出现我方",
}


def format_issues(issues, max_show=3):
    """把 issues 拼成可读原因（用于不通过原因列与日志）"""
    if not issues:
        return None
    parts = []
    for it in issues[:max_show]:
        lab = TYPE_LABEL.get(it.get("type"), "观点「%s」不合规" % it.get("viewpoint", "?"))
        vp = (it.get("viewpoint") or "").strip()
        if vp and "%s" in lab:
            lab = lab % vp
        det = (it.get("detail") or "").strip()
        parts.append("%s：%s" % (lab, det) if det else lab)
    extra = len(issues) - max_show
    if extra > 0:
        parts.append("等%d处问题" % extra)
    return "；".join(parts)
