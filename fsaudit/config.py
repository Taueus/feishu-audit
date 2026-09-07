# -*- coding: utf-8 -*-
"""配置读写与解析"""
import os
import re

import yaml

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_PATH = os.path.join(BASE_DIR, "config.yaml")

DEFAULT_COLUMNS = {
    "keyword": "项目",      # 我方关键词（多个）
    "link": "回链",        # 文档附件列（只审 .doc/.docx 附件，其他链接/空值跳过）
    "result": "机器审核",   # 审核结果
    "reason": "不通过原因",  # 不通过原因
}
DEFAULT_FORBIDDEN = ["AI生成", "免责声明"]
DOC_EXTS = [".docx", ".doc"]   # 回链列只审这些扩展名的附件

# 每条规则的启停开关（True=启用；False=跳过该规则的判罚）。可在管理面板独立配置。
# 规则四/五由 LLM 判定，会产生 API 调用费用；规则一/二/三为本地确定性规则。
DEFAULT_RULES_ENABLED = {
    "r1": True,  # 规则一·AI 痕迹词（确定性）
    "r2": True,  # 规则二·违禁词（确定性）
    "r3": True,  # 规则三·关键词位置（确定性，我方须先于竞品出现）
    "r4": True,  # 规则四·观点级主角性（LLM）
    "r5": True,  # 规则五·AI 人味 & AI 收录友好度（LLM+启发式）
}


class LLMConfig(object):
    def __init__(self):
        self.base_url = "https://api.deepseek.com"
        self.api_key = ""
        self.model = "deepseek-chat"

    def to_dict(self):
        return {"base_url": self.base_url, "api_key": self.api_key, "model": self.model}

    @classmethod
    def from_dict(cls, d):
        c = cls()
        if d:
            c.base_url = d.get("base_url", c.base_url)
            c.api_key = d.get("api_key", "")
            c.model = d.get("model", c.model)
        return c


class AppConfig(object):
    def __init__(self):
        self.app_id = ""
        self.app_secret = ""
        self.spreadsheet_token = ""
        self.folder_token = ""
        self.llm = LLMConfig()
        self.columns = dict(DEFAULT_COLUMNS)
        self.forbidden_words = list(DEFAULT_FORBIDDEN)
        # 每条审核规则的启停开关。False 时该规则直接跳过（不参与判罚）。
        self.rules_enabled = dict(DEFAULT_RULES_ENABLED)


def parse_spreadsheet_token(s):
    """从链接或裸 token 解析电子表格 token"""
    s = (s or "").strip()
    m = re.search(r"/sheets/([A-Za-z0-9]+)", s)
    if m:
        return m.group(1)
    if re.fullmatch(r"[A-Za-z0-9]{15,}", s):
        return s
    raise ValueError("无法解析表格 token，请粘贴形如 https://xxx.feishu.cn/sheets/xxxxx 的链接"
                    "（注意：短链接请先在浏览器打开后再复制完整地址）")


def parse_folder_token(s):
    """从链接或裸 token 解析云空间文件夹 token"""
    s = (s or "").strip()
    if not s:
        return ""
    m = re.search(r"/folder/([A-Za-z0-9]+)", s)
    if m:
        return m.group(1)
    if re.fullmatch(r"[A-Za-z0-9]{15,}", s):
        return s
    raise ValueError("无法解析文件夹 token，请粘贴形如 https://xxx.feishu.cn/drive/folder/xxxxx 的链接")


def load_config(path=None):
    path = path or CONFIG_PATH
    with open(path, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}
    cfg = AppConfig()
    cfg.app_id = raw.get("app_id", "")
    cfg.app_secret = raw.get("app_secret", "")
    cfg.spreadsheet_token = raw.get("spreadsheet_token", "")
    cfg.folder_token = raw.get("folder_token", "")
    cfg.llm = LLMConfig.from_dict(raw.get("llm") or {})
    cols = raw.get("columns") or {}
    for k in DEFAULT_COLUMNS:
        if cols.get(k):
            cfg.columns[k] = cols[k]
    if raw.get("forbidden_words"):
        cfg.forbidden_words = list(raw["forbidden_words"])
    # 规则启停开关：未在 yaml 中给出的项保持默认（True）；显式 false 才关
    rules_raw = raw.get("rules_enabled") or {}
    if isinstance(rules_raw, dict):
        for k, v in rules_raw.items():
            if k in DEFAULT_RULES_ENABLED:
                cfg.rules_enabled[k] = bool(v)
    return cfg


def save_config(cfg, path=None):
    path = path or CONFIG_PATH
    data = {
        "app_id": cfg.app_id,
        "app_secret": cfg.app_secret,
        "spreadsheet_token": cfg.spreadsheet_token,
        "folder_token": cfg.folder_token,
        "llm": cfg.llm.to_dict(),
        "columns": cfg.columns,
        "forbidden_words": cfg.forbidden_words,
        "rules_enabled": cfg.rules_enabled,
    }
    with open(path, "w", encoding="utf-8") as f:
        yaml.safe_dump(data, f, allow_unicode=True, sort_keys=False)
