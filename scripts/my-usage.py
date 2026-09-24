#!/usr/bin/env python3
"""
用「自己的 key」查自己的限速、預算與已花費 —— 不需要管理員金鑰。

用法：
  LITELLM_API_KEY=sk-你的key python3 scripts/my-usage.py
  python3 scripts/my-usage.py sk-你的key

環境變數：
  LITELLM_URL   預設 http://localhost:4000
只用 Python 標準函式庫，發給 agent 主機直接跑即可。
"""
import json
import os
import sys
import unicodedata
import urllib.error
import urllib.parse
import urllib.request

BASE = os.environ.get("LITELLM_URL", "http://localhost:4000").rstrip("/")


def get(key, path, **params):
    url = BASE + path + ("?" + urllib.parse.urlencode(params) if params else "")
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {key}"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            sys.exit("✘ key 無效或已被刪除")
        if e.code == 429:
            # 超額後 LiteLLM 連查詢 API 也會擋，只能從錯誤訊息看是哪一層
            try:
                msg = json.loads(e.read())["error"]["message"]
            except (ValueError, KeyError, TypeError):
                msg = "預算已用完"
            sys.exit(f"⛔ 已達上限，這把 key 目前所有請求都會被拒絕\n"
                     f"   {msg}\n"
                     f"   等預算週期重置，或請管理員調高 access/teams.yaml 裡的 budget")
        return None
    except urllib.error.URLError as e:
        sys.exit(f"✘ 連不上 {BASE}：{e.reason}")


def money(v):
    return "不限" if v is None else f"${v:,.2f}"


def num(v):
    return "不限" if v is None else f"{v:,}"


def usage(spent, limit):
    if not limit:
        return ""
    pct = spent / limit * 100
    mark = "  ⛔ 已達上限，請求會被拒絕" if pct >= 100 else "  ⚠ 接近上限" if pct >= 80 else ""
    return f"（{pct:.0f}%{mark}）"


def row(label, text):
    pad = " " * max(1, 10 - sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in label))
    print(f"  {label}{pad}{text}")


def line(label, spent, limit, period=None, reset=None):
    extra = ""
    if limit and period:
        extra = f"，每 {period} 重置" + (f"，下次 {reset[:10]}" if reset else "")
    row(label, f"{money(spent)} / {money(limit)}{usage(spent, limit)}{extra}")


def main():
    key = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("LITELLM_API_KEY")
    if not key:
        sys.exit(__doc__.strip())

    k = (get(key, "/key/info") or {}).get("info") or {}
    uid, tid = k.get("user_id"), k.get("team_id")

    print(f"■ 這把 key：{k.get('key_alias') or k.get('key_name')}")
    line("花費", k.get("spend") or 0, k.get("max_budget"), k.get("budget_duration"), k.get("budget_reset_at"))
    row("RPM / TPM", f"{num(k.get('rpm_limit'))} / {num(k.get('tpm_limit'))}")
    if k.get("models"):
        row("模型", ", ".join(k["models"]))

    if tid:
        info = get(key, "/team/info", team_id=tid) or {}
        t = info.get("team_info") or {}
        print(f"\n■ 所屬團隊：{t.get('team_alias') or tid}")
        line("全隊", t.get("spend") or 0, t.get("max_budget"), t.get("budget_duration"), t.get("budget_reset_at"))
        # 只顯示自己那一列，不列出其他成員的花費
        mine = next((m for m in info.get("team_memberships") or [] if m.get("user_id") == uid), None)
        if mine:
            bt = mine.get("litellm_budget_table") or {}
            line("我在本隊", mine.get("spend") or 0, bt.get("max_budget"),
                 bt.get("budget_duration"), bt.get("budget_reset_at"))
        row("RPM / TPM", f"{num(t.get('rpm_limit'))} / {num(t.get('tpm_limit'))}（全隊共用）")
        row("可用模型", ", ".join(t.get("models") or []) or "不限")

    if uid:
        u = (get(key, "/user/info", user_id=uid) or {}).get("user_info") or {}
        print(f"\n■ 我（{uid}）")
        row("RPM / TPM", f"{num(u.get('rpm_limit'))} / {num(u.get('tpm_limit'))}（我所有 key 加總）")

    print("\n實際可用額度 = 以上各層中最嚴格的那一個")


if __name__ == "__main__":
    main()
