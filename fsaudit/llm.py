# -*- coding: utf-8 -*-
"""LLM 品牌实体识别（仅识别，不判定；结果本地缓存）"""
import hashlib
import json
import os
import re

import requests

from .config import LLMConfig

SYSTEM_PROMPT = (
    "你是品牌实体识别器。从用户给出的文章中提取所有出现的品牌名、产品名、公司名"
    "（包括我方品牌和竞品品牌）。只输出一个JSON字符串数组，例如 [\"品牌A\",\"品牌B\"]，"
    "不要输出任何解释、前后缀或其他内容。没有品牌时输出 []。"
)


class BrandIdentifier(object):
    def __init__(self, llm_cfg, cache_dir="cache"):
        # 兼容直接传 dict（旧调用方式）
        if isinstance(llm_cfg, dict):
            c = LLMConfig()
            c.base_url = llm_cfg.get("base_url", c.base_url)
            c.api_key = llm_cfg.get("api_key", "")
            c.model = llm_cfg.get("model", c.model)
            llm_cfg = c
        self.cfg = llm_cfg
        os.makedirs(cache_dir, exist_ok=True)
        self.cache_path = os.path.join(cache_dir, "brand_cache.json")
        self._cache = {}
        if os.path.exists(self.cache_path):
            try:
                with open(self.cache_path, "r", encoding="utf-8") as f:
                    self._cache = json.load(f)
            except (ValueError, OSError):
                self._cache = {}

    def available(self):
        return bool(self.cfg.api_key)

    def _chat(self, messages, max_tokens=2048, temperature=0):
        r = requests.post(
            self.cfg.base_url.rstrip("/") + "/chat/completions",
            headers={"Authorization": "Bearer " + self.cfg.api_key,
                     "Content-Type": "application/json"},
            json={"model": self.cfg.model, "temperature": temperature,
                  "max_tokens": max_tokens, "messages": messages},
            timeout=180,
        )
        if r.status_code != 200:
            raise RuntimeError("LLM HTTP %d：%s" % (r.status_code, r.text[:200]))
        return r.json()["choices"][0]["message"]["content"]

    def identify(self, text):
        """返回文中出现的品牌实体列表；未配置 api_key 时返回 []（纯词库模式）"""
        if not self.available():
            return []
        key = hashlib.md5(text.encode("utf-8")).hexdigest()
        if key in self._cache:
            return list(self._cache[key])
        content = self._chat([
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": text[:60000]},
        ])
        m = re.search(r"\[.*\]", content, re.S)
        if not m:
            raise RuntimeError("LLM 返回无法解析为JSON数组：%s" % content[:200])
        brands = [str(b).strip() for b in json.loads(m.group(0)) if str(b).strip()]
        self._cache[key] = brands
        try:
            with open(self.cache_path, "w", encoding="utf-8") as f:
                json.dump(self._cache, f, ensure_ascii=False)
        except OSError:
            pass
        return list(brands)

    def selfcheck(self):
        """只验证连通性：能返回 200 并有内容即通过"""
        self._chat([{"role": "user", "content": "回复OK"}], max_tokens=16)
