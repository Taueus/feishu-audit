# -*- coding: utf-8 -*-
"""飞书开放平台 API 封装：认证 / Sheets / Drive"""
import time

import requests

BASE = "https://open.feishu.cn/open-apis"
TIMEOUT = 60


class FeishuError(Exception):
    pass


def _check(resp):
    try:
        data = resp.json()
    except ValueError:
        raise FeishuError("接口返回非 JSON（HTTP %d）：%s" % (resp.status_code, resp.text[:200]))
    code = data.get("code", 0)
    if code != 0:
        raise FeishuError("code=%s msg=%s" % (code, data.get("msg")))
    return data.get("data", {})


class Feishu(object):
    def __init__(self, app_id, app_secret):
        self.app_id = app_id
        self.app_secret = app_secret
        self._token = ""
        self._expire = 0.0

    # ---------- 基础 ----------
    def token(self):
        if time.time() < self._expire - 120:
            return self._token
        r = requests.post(BASE + "/auth/v3/tenant_access_token/internal",
                          json={"app_id": self.app_id, "app_secret": self.app_secret},
                          timeout=TIMEOUT)
        data = r.json()
        if data.get("code") != 0:
            raise FeishuError("获取 tenant_access_token 失败：%s" % data.get("msg"))
        self._token = data["tenant_access_token"]
        self._expire = time.time() + int(data.get("expire", 7200))
        return self._token

    def _headers(self):
        return {"Authorization": "Bearer " + self.token()}

    def _retry(self, fn, tries=3):
        last = None
        for i in range(tries):
            try:
                return fn()
            except requests.RequestException as e:
                last = e
                time.sleep(1 + i)
        raise FeishuError("网络请求失败：%s" % last)

    def _get(self, url, params=None):
        def _do():
            return _check(requests.get(url, headers=self._headers(), params=params, timeout=TIMEOUT))
        return self._retry(_do)

    def _put(self, url, body):
        def _do():
            return _check(requests.put(url, headers=self._headers(), json=body, timeout=TIMEOUT))
        return self._retry(_do)

    def _post(self, url, body):
        def _do():
            return _check(requests.post(url, headers=self._headers(), json=body, timeout=TIMEOUT))
        return self._retry(_do)

    # ---------- 电子表格 ----------
    def list_sheets(self, spreadsheet_token):
        """返回 [{sheet_id, title}]"""
        data = self._get(BASE + "/sheets/v3/spreadsheets/%s/sheets/query" % spreadsheet_token)
        return [{"sheet_id": it["sheet_id"], "title": it.get("title", "")}
                for it in (data.get("sheets") or [])]

    def read_range(self, spreadsheet_token, range_str, formula=False):
        """读取 range，返回 list[list]。
        formula=False: 纯文本（附件单元格返回 [{'id':1,'type':'attachment'}]）
        formula=True : 公式计算值（附件单元格返回含 fileToken/文件名的完整信息）"""
        import urllib.parse
        url = BASE + "/sheets/v2/spreadsheets/%s/values/%s" % (
            spreadsheet_token, urllib.parse.quote(range_str, safe=""))
        opt = "Formula" if formula else "ToString"
        data = self._get(url, params={"valueRenderOption": opt})
        return [[("" if c is None else c) for c in row]
                for row in (data.get("valueRange", {}).get("values") or [])]

    @staticmethod
    def cell_attachments(value):
        """从公式值单元格提取附件列表 [(file_token, filename)]，无附件返回 []"""
        if not isinstance(value, list):
            return []
        out = []
        for item in value:
            if isinstance(item, dict) and item.get("type") == "attachment" \
                    and item.get("fileToken"):
                out.append((item["fileToken"], str(item.get("text") or "")))
        return out

    def read_header(self, spreadsheet_token, sheet_id):
        rows = self.read_range(spreadsheet_token, "%s!A1:ZZ1" % sheet_id)
        return [str(c or "").strip() for c in rows[0]] if rows else []

    def read_all(self, spreadsheet_token, sheet_id, max_rows=5000):
        """按表头实际宽度分块读取，避免请求超出 10MB 限制"""
        header = self.read_header(spreadsheet_token, sheet_id)
        # 去掉尾部空列
        ncol = len(header)
        while ncol > 0 and not header[ncol - 1]:
            ncol -= 1
        ncol = max(ncol, 1)
        letter = ""
        n = ncol
        while n > 0:
            n, r = divmod(n - 1, 26)
            letter = chr(65 + r) + letter
        rows = []
        step = 300   # 每块 300 行
        start = 1
        while start <= max_rows:
            end = min(start + step - 1, max_rows)
            part = self.read_range(spreadsheet_token,
                                   "%s!A%s:%s%d" % (sheet_id, start, letter, end))
            if not part:
                break
            rows.extend(part)
            if len(part) < (end - start + 1):
                break
            start = end + 1
        return rows

    def write_cell(self, spreadsheet_token, sheet_id, a1, value):
        """写入单个单元格，如 write_cell(tok, sid, 'C5', '通过')
        注意：写入 API 不接受 'C5' 单格简写，必须是 'C5:C5' 完整范围格式"""
        if ":" not in a1:
            a1 = a1 + ":" + a1
        url = BASE + "/sheets/v2/spreadsheets/%s/values" % spreadsheet_token
        body = {"valueRange": {"range": "%s!%s" % (sheet_id, a1), "values": [[value]]}}
        self._put(url, body)

    # ---------- 云空间 ----------
    def list_folder(self, folder_token):
        """列取文件夹全部文件，返回 {文件名小写: file_token}"""
        result = {}
        page_token = ""
        while True:
            params = {"folder_token": folder_token, "page_size": 200}
            if page_token:
                params["page_token"] = page_token
            data = self._get(BASE + "/drive/v1/files", params=params)
            for f in (data.get("files") or []):
                name = f.get("name", "")
                if name and f.get("token"):
                    result[name.lower()] = f["token"]
            page_token = data.get("page_token") or ""
            if not data.get("has_more") or not page_token:
                break
        return result

    def search_file(self, name):
        """全盘搜索同名文件，返回 [file_token]"""
        data = self._post(BASE + "/suite/docs-api/search/object",
                          {"search_key": name, "count": 20, "offset": 0})
        tokens = []
        for e in (data.get("entities") or []):
            title = (e.get("title") or "").strip()
            if title == name.strip():
                tokens.append(e["obj_token"])
        return tokens

    def download(self, file_token, save_path):
        url = BASE + "/drive/v1/medias/%s/download" % file_token
        with requests.get(url, headers=self._headers(), stream=True, timeout=300) as r:
            if r.status_code != 200:
                raise FeishuError("下载失败 HTTP %d" % r.status_code)
            with open(save_path, "wb") as f:
                for chunk in r.iter_content(65536):
                    if chunk:
                        f.write(chunk)
