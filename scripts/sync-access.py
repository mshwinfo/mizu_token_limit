#!/usr/bin/env python3
"""
把 access/teams.yaml 的團隊 / 人員 / key 限制同步到 LiteLLM。

用法：
  python3 scripts/sync-access.py validate          # 只檢查設定檔，不連線
  python3 scripts/sync-access.py plan              # 列出會做的變更，不寫入
  python3 scripts/sync-access.py apply             # 套用變更
  python3 scripts/sync-access.py apply --prune     # 另外移除設定檔裡已刪掉的成員 / key
  python3 scripts/sync-access.py report            # 每隊、每人、每把 key 的用量 vs 上限

環境變數（沒設的話會讀 repo 根目錄的 .env）：
  LITELLM_MASTER_KEY   必填（validate 除外）
  LITELLM_URL          預設 http://localhost:4000

依賴：PyYAML（pip install pyyaml）
"""
import argparse
import json
import os
import re
import sys
import unicodedata
import urllib.error
import urllib.parse
import urllib.request

try:
    import yaml
except ImportError:
    sys.exit("缺少 PyYAML：pip install pyyaml")

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_SPEC = os.path.join(REPO, "access", "teams.yaml")
LITELLM_CONFIG = os.path.join(REPO, "litellm", "config.yaml")

ROLE_MAP = {"user": "internal_user", "viewer": "internal_user_viewer", "admin": "proxy_admin"}
PERIOD_RE = re.compile(r"^\d+(s|m|h|d|mo)$")
MANAGED_TAG = "sync-access"

TOP_FIELDS = {"budget_period", "users", "teams"}
USER_FIELDS = {"email", "role", "rpm", "tpm"}
TEAM_FIELDS = {"name", "models", "budget", "budget_period", "rpm", "tpm", "member_budget", "members"}
MEMBER_FIELDS = {"budget", "budget_period", "keys"}
KEY_FIELDS = {"rpm", "tpm", "budget", "budget_period", "models"}


# ============================================================
#  讀取與驗證設定檔
# ============================================================

class SpecError(Exception):
    pass


def _check_fields(where, obj, allowed):
    if obj is None:
        return {}
    if not isinstance(obj, dict):
        raise SpecError(f"{where}：應該是 key: value 的對應表")
    unknown = set(obj) - allowed
    if unknown:
        raise SpecError(f"{where}：不認得的欄位 {sorted(unknown)}（可用：{sorted(allowed)}）")
    return obj


def _positive(where, value, kind):
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        raise SpecError(f"{where}：{kind} 必須是大於 0 的數字，拿到 {value!r}")
    if kind in ("rpm", "tpm") and not float(value).is_integer():
        raise SpecError(f"{where}：{kind} 必須是整數，拿到 {value!r}")
    return int(value) if kind in ("rpm", "tpm") else float(value)


def _period(where, value):
    if value is None:
        return None
    if not isinstance(value, str) or not PERIOD_RE.match(value):
        raise SpecError(f"{where}：budget_period 格式應為 30d / 7d / 1d / 1mo，拿到 {value!r}")
    return value


def _models(where, value):
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(m, str) for m in value):
        raise SpecError(f"{where}：models 應該是模型名稱的清單")
    return value


def load_spec(path):
    """讀設定檔並攤平成 users / teams / members / keys 四張表。"""
    with open(path, encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or {}
    _check_fields("最上層", raw, TOP_FIELDS)
    default_period = _period("budget_period", raw.get("budget_period")) or "30d"

    users = {}
    for uid, u in (raw.get("users") or {}).items():
        where = f"users.{uid}"
        u = _check_fields(where, u, USER_FIELDS)
        role = u.get("role", "user")
        if role not in ROLE_MAP:
            raise SpecError(f"{where}：role 只能是 {sorted(ROLE_MAP)}，拿到 {role!r}")
        users[str(uid)] = {
            "email": u.get("email"),
            "role": ROLE_MAP[role],
            "rpm": _positive(where, u.get("rpm"), "rpm"),
            "tpm": _positive(where, u.get("tpm"), "tpm"),
        }

    teams, members, keys = {}, {}, {}
    for tid, t in (raw.get("teams") or {}).items():
        tid = str(tid)
        where = f"teams.{tid}"
        t = _check_fields(where, t, TEAM_FIELDS)
        team_models = _models(where, t.get("models"))
        team_period = _period(where, t.get("budget_period")) or default_period
        member_budget = _positive(where, t.get("member_budget"), "member_budget")
        budget = _positive(where, t.get("budget"), "budget")
        teams[tid] = {
            "name": t.get("name") or tid,
            "models": team_models,
            "budget": budget,
            "budget_period": team_period if budget else None,
            "rpm": _positive(where, t.get("rpm"), "rpm"),
            "tpm": _positive(where, t.get("tpm"), "tpm"),
        }

        for uid, m in (t.get("members") or {}).items():
            uid = str(uid)
            mwhere = f"{where}.members.{uid}"
            m = _check_fields(mwhere, m, MEMBER_FIELDS)
            if uid not in users:
                raise SpecError(f"{mwhere}：{uid} 沒有在 users 裡定義")
            mbudget = _positive(mwhere, m.get("budget"), "budget") or member_budget
            members[(tid, uid)] = {
                "budget": mbudget,
                "budget_period": (_period(mwhere, m.get("budget_period")) or team_period) if mbudget else None,
            }

            for alias, k in (m.get("keys") or {}).items():
                alias = str(alias)
                kwhere = f"{mwhere}.keys.{alias}"
                k = _check_fields(kwhere, k, KEY_FIELDS)
                if alias in keys:
                    other = keys[alias]
                    raise SpecError(f"{kwhere}：key 別名重複（已用在 teams.{other['team']}.members.{other['user']}）")
                key_models = _models(kwhere, k.get("models"))
                if team_models and set(key_models) - set(team_models):
                    raise SpecError(f"{kwhere}：models {sorted(set(key_models) - set(team_models))} "
                                    f"不在團隊允許的 {team_models} 裡")
                kbudget = _positive(kwhere, k.get("budget"), "budget")
                keys[alias] = {
                    "team": tid,
                    "user": uid,
                    "models": key_models,
                    "rpm": _positive(kwhere, k.get("rpm"), "rpm"),
                    "tpm": _positive(kwhere, k.get("tpm"), "tpm"),
                    "budget": kbudget,
                    "budget_period": (_period(kwhere, k.get("budget_period")) or team_period) if kbudget else None,
                }

    return {"users": users, "teams": teams, "members": members, "keys": keys}


def referenced_models(spec):
    names = set()
    for t in spec["teams"].values():
        names.update(t["models"])
    for k in spec["keys"].values():
        names.update(k["models"])
    return names


def config_model_names():
    try:
        with open(LITELLM_CONFIG, encoding="utf-8") as fh:
            cfg = yaml.safe_load(fh) or {}
    except OSError:
        return set()
    return {m.get("model_name") for m in cfg.get("model_list") or [] if isinstance(m, dict)}


# ============================================================
#  LiteLLM API
# ============================================================

class ApiError(Exception):
    pass


class Client:
    def __init__(self, base, master_key):
        self.base = base.rstrip("/")
        self.headers = {"Authorization": f"Bearer {master_key}", "Content-Type": "application/json"}

    def _call(self, method, path, params=None, body=None):
        url = self.base + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, headers=self.headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return resp.status, json.loads(resp.read() or b"null")
        except urllib.error.HTTPError as e:
            raw = e.read().decode(errors="replace")
            try:
                return e.code, json.loads(raw)
            except ValueError:
                return e.code, raw
        except urllib.error.URLError as e:
            raise ApiError(f"連不上 {self.base}：{e.reason}") from None

    def get(self, path, **params):
        status, data = self._call("GET", path, params=params)
        if status == 404:
            return None
        if status != 200:
            raise ApiError(f"GET {path} {params} → {status}: {data}")
        return data

    def post(self, path, body):
        status, data = self._call("POST", path, body=body)
        if status != 200:
            raise ApiError(f"POST {path} → {status}: {data}")
        return data


def load_env():
    """環境變數優先；沒設的話從 repo 根目錄的 .env 補。"""
    path = os.path.join(REPO, ".env")
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip("'\""))


def make_client():
    load_env()
    key = os.environ.get("LITELLM_MASTER_KEY")
    if not key:
        sys.exit("請先設定 LITELLM_MASTER_KEY（export 或寫在 .env）")
    return Client(os.environ.get("LITELLM_URL", "http://localhost:4000"), key)


# ============================================================
#  計算差異
# ============================================================

class Plan:
    def __init__(self):
        self.actions = []     # (說明, path, body, 是否為新 key)
        self.warnings = []

    def add(self, desc, path, body, new_key=False):
        self.actions.append((desc, path, body, new_key))


def _same(have, want):
    if isinstance(want, list) or isinstance(have, list):
        return set(have or []) == set(want or [])
    if isinstance(have, (int, float)) and isinstance(want, (int, float)):
        return float(have) == float(want)
    return have == want


def _diff(current, desired, labels):
    """回傳有變動的欄位 {api 欄位: 新值}，以及給人看的變動描述。"""
    changed, desc = {}, []
    for field, want in desired.items():
        have = current.get(field)
        if not _same(have, want):
            changed[field] = want
            desc.append(f"{labels.get(field, field)} {_fmt(have, field)} → {_fmt(want, field)}")
    return changed, desc


MONEY_FIELDS = {"max_budget", "max_budget_in_team"}


def _fmt(v, field=None):
    if v is None or v == []:
        return "不限"
    if isinstance(v, list):
        return ",".join(v)
    if field in MONEY_FIELDS:
        return _money(v)
    return str(v)


def build_plan(api, spec, prune):
    plan = Plan()
    labels = {
        "user_email": "email", "user_role": "角色", "rpm_limit": "RPM", "tpm_limit": "TPM",
        "team_alias": "名稱", "models": "模型", "max_budget": "預算", "budget_duration": "週期",
        "max_budget_in_team": "本隊預算",
    }

    # ---- 模型必須存在 ----
    listed = api.get("/v1/models") or {}
    available = {m["id"] for m in listed.get("data", [])}
    missing = referenced_models(spec) - available
    if missing:
        raise SpecError(f"這些模型在 LiteLLM 裡不存在：{sorted(missing)}；"
                        f"請先加進 litellm/config.yaml 或 Web UI（目前有：{sorted(available)}）")

    # ---- 人員 ----
    for uid, u in spec["users"].items():
        desired = {"user_email": u["email"], "user_role": u["role"],
                   "rpm_limit": u["rpm"], "tpm_limit": u["tpm"]}
        info = api.get("/user/info", user_id=uid)
        current = (info or {}).get("user_info") or {}
        if not current:
            body = {"user_id": uid, "auto_create_key": False, **{k: v for k, v in desired.items() if v is not None}}
            plan.add(f"新增人員 {uid}", "/user/new", body)
            continue
        changed, desc = _diff(current, desired, labels)
        for field in ("rpm_limit", "tpm_limit"):
            if field in changed and changed[field] is None:
                # 實測 /user/update 送 null 不會清掉個人限速
                plan.warnings.append(f"人員 {uid}：LiteLLM API 無法清除個人 {labels[field]}"
                                     f"（目前 {current.get(field)}），請到 Web UI 手動移除，或改設一個夠大的數字")
                del changed[field]
                desc = [d for d in desc if not d.startswith(labels[field] + " ")]
        if changed:
            plan.add(f"更新人員 {uid}：" + "；".join(desc), "/user/update", {"user_id": uid, **changed})

    # ---- 團隊 ----
    team_state = {}
    for tid, t in spec["teams"].items():
        desired = {"team_alias": t["name"], "models": t["models"], "max_budget": t["budget"],
                   "budget_duration": t["budget_period"], "rpm_limit": t["rpm"], "tpm_limit": t["tpm"]}
        info = api.get("/team/info", team_id=tid)
        team_state[tid] = info
        if not info:
            body = {"team_id": tid, **{k: v for k, v in desired.items() if v not in (None, [])}}
            plan.add(f"新增團隊 {tid}（{t['name']}）", "/team/new", body)
            continue
        changed, desc = _diff(info.get("team_info") or {}, desired, labels)
        if changed:
            plan.add(f"更新團隊 {tid}：" + "；".join(desc), "/team/update", {"team_id": tid, **changed})

    # ---- 團隊成員 ----
    removed_members = set()
    for tid in spec["teams"]:
        info = team_state[tid] or {}
        roles = {m.get("user_id"): m.get("role") for m in (info.get("team_info") or {}).get("members_with_roles") or []}
        budgets = {}
        for ms in info.get("team_memberships") or []:
            bt = ms.get("litellm_budget_table") or {}
            budgets[ms.get("user_id")] = {"max_budget_in_team": bt.get("max_budget"),
                                          "budget_duration": bt.get("budget_duration")}
        wanted = {uid for (t, uid) in spec["members"] if t == tid}

        for uid in sorted(wanted):
            m = spec["members"][(tid, uid)]
            desired = {"max_budget_in_team": m["budget"], "budget_duration": m["budget_period"]}
            if uid not in roles:
                body = {"team_id": tid, "member": {"role": "user", "user_id": uid}}
                body.update({k: v for k, v in desired.items() if v is not None})
                plan.add(f"{uid} 加入 {tid}（本隊預算 {_money(m['budget'])}）", "/team/member_add", body)
                continue
            changed, desc = _diff(budgets.get(uid, {}), desired, labels)
            if changed:
                plan.add(f"{uid} 在 {tid}：" + "；".join(desc), "/team/member_update",
                         {"team_id": tid, "user_id": uid, **desired})

        extra = sorted(uid for uid, role in roles.items()
                       if uid not in wanted and role != "admin" and uid != "default_user_id")
        for uid in extra:
            # LiteLLM 移除成員時會一併刪掉他在這個團隊的所有 key（已實測）
            n_keys = sum(1 for x in info.get("keys") or [] if x.get("user_id") == uid)
            also = f"，連同他在本隊的 {n_keys} 把 key 一起刪除" if n_keys else ""
            if prune:
                plan.add(f"{uid} 移出 {tid}{also}", "/team/member_delete", {"team_id": tid, "user_id": uid})
                removed_members.add((tid, uid))
            else:
                plan.warnings.append(f"{uid} 在 {tid} 裡但設定檔沒有列（加 --prune 才會移除{also}）")

    # ---- Key ----
    for alias, k in spec["keys"].items():
        desired = {"models": k["models"], "rpm_limit": k["rpm"], "tpm_limit": k["tpm"],
                   "max_budget": k["budget"], "budget_duration": k["budget_period"]}
        found = api.get("/key/list", key_alias=alias, return_full_object="true") or {}
        existing = [x for x in found.get("keys", []) if x.get("key_alias") == alias]
        if not existing:
            body = {"key_alias": alias, "team_id": k["team"], "user_id": k["user"],
                    "metadata": {"managed_by": MANAGED_TAG, "team": k["team"], "host": alias},
                    **{f: v for f, v in desired.items() if v not in (None, [])}}
            plan.add(f"發新 key {alias}（{k['team']} / {k['user']}，RPM {_fmt(k['rpm'])}）",
                     "/key/generate", body, new_key=True)
            continue
        cur = existing[0]
        if cur.get("team_id") != k["team"] or cur.get("user_id") != k["user"]:
            raise SpecError(f"key {alias} 目前屬於 {cur.get('team_id')} / {cur.get('user_id')}，"
                            f"設定檔寫 {k['team']} / {k['user']}；不會自動搬移，請改別名或到 Web UI 處理")
        changed, desc = _diff(cur, desired, labels)
        if changed:
            plan.add(f"更新 key {alias}：" + "；".join(desc), "/key/update", {"key": cur["token"], **changed})

    # 由本腳本發出、但已從設定檔刪掉的 key
    for tid in spec["teams"]:
        for x in (team_state[tid] or {}).get("keys") or []:
            alias = x.get("key_alias")
            managed = (x.get("metadata") or {}).get("managed_by") == MANAGED_TAG
            if not managed or alias in spec["keys"] or (tid, x.get("user_id")) in removed_members:
                continue
            if prune:
                plan.add(f"刪除 key {alias}", "/key/delete", {"keys": [x["token"]]})
            else:
                plan.warnings.append(f"key {alias} 已不在設定檔（加 --prune 才會刪除）")

    return plan


# ============================================================
#  指令
# ============================================================

def cmd_validate(args):
    spec = load_spec(args.file)
    known = config_model_names()
    unknown = referenced_models(spec) - known
    print(f"✔ {args.file} 格式正確："
          f"{len(spec['users'])} 人、{len(spec['teams'])} 隊、{len(spec['keys'])} 把 key")
    if unknown:
        print(f"  ⚠ 模型 {sorted(unknown)} 不在 litellm/config.yaml —— "
              f"若是在 Web UI 加的沒關係，apply 時會再向 LiteLLM 確認")


def cmd_plan(args, apply=False):
    spec = load_spec(args.file)
    api = make_client()
    plan = build_plan(api, spec, args.prune)

    for w in plan.warnings:
        print(f"⚠ {w}")
    if not plan.actions:
        print("✔ 已經同步，沒有需要變更的地方")
        return
    print(("套用" if apply else "預計") + f" {len(plan.actions)} 項變更：")

    new_keys = []
    for desc, path, body, is_new_key in plan.actions:
        print(f"  • {desc}")
        if not apply:
            continue
        result = api.post(path, body)
        if is_new_key:
            new_keys.append((body["key_alias"], result["key"]))

    if not apply:
        print("\n（plan 模式，沒有寫入。確認沒問題後執行 apply）")
        return
    print("✔ 完成")
    if new_keys:
        print("\n新發的 key（只會顯示這一次，請立刻存進 secret store）：")
        for alias, key in new_keys:
            print(f"  {alias}  {key}")
        if args.keys_file:
            fd = os.open(args.keys_file, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            with os.fdopen(fd, "a") as fh:
                for alias, key in new_keys:
                    fh.write(f"{alias}={key}\n")
            print(f"  （已附加寫入 {args.keys_file}，權限 600）")


def _width(s):
    return sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in s)


def _row(cells, widths):
    return "  ".join(c + " " * (w - _width(c)) for c, w in zip(cells, widths)).rstrip()


def _table(header, rows, indent="  "):
    widths = [max(_width(r[i]) for r in [header] + rows) for i in range(len(header))]
    print(indent + _row(header, widths))
    for r in rows:
        print(indent + _row(r, widths))


def _money(v):
    return "不限" if v is None else f"${v:,.2f}"


def _num(v):
    return "不限" if v is None else f"{v:,}"


def _usage(spent, limit):
    if not limit:
        return "-"
    pct = spent / limit * 100
    mark = "  ⛔ 已達上限" if pct >= 100 else "  ⚠ 接近上限" if pct >= 80 else ""
    return f"{pct:.0f}%{mark}"


def cmd_report(args):
    api = make_client()
    teams = api.get("/team/list") or []
    if args.team:
        teams = [t for t in teams if t.get("team_id") in args.team]
    users_seen = set()
    for t in sorted(teams, key=lambda x: (x.get("team_alias") or x.get("team_id") or "")):
        tid = t["team_id"]
        info = api.get("/team/info", team_id=tid) or {}
        ti = info.get("team_info") or {}
        keys = info.get("keys") or []
        spent = ti.get("spend") or 0
        reset = ti.get("budget_reset_at")
        reset_s = f"，{reset[:10]} 重置" if reset else ""
        print(f"\n■ {ti.get('team_alias') or tid}（{tid}）")
        if ti.get("max_budget"):
            print(f"  花費 {_money(spent)} / {_money(ti['max_budget'])}  {_usage(spent, ti['max_budget'])}"
                  f"（{ti.get('budget_duration') or '不重置'}{reset_s}）")
        else:
            print(f"  花費 {_money(spent)}（不限預算）")
        print(f"  RPM {_num(ti.get('rpm_limit'))}   TPM {_num(ti.get('tpm_limit'))}"
              f"   模型 {_fmt(ti.get('models'))}")

        # 沒設個人預算的成員不會有 membership 紀錄，花費改用他名下 key 加總
        memberships = {ms["user_id"]: ms for ms in info.get("team_memberships") or []}
        member_ids = [m["user_id"] for m in ti.get("members_with_roles") or []
                      if m.get("user_id") and m.get("user_id") != "default_user_id"]
        rows = []
        for uid in sorted(set(member_ids) | set(memberships)):
            ms = memberships.get(uid) or {}
            limit = (ms.get("litellm_budget_table") or {}).get("max_budget")
            spent_u = ms.get("spend")
            if spent_u is None:
                spent_u = sum(k.get("spend") or 0 for k in keys if k.get("user_id") == uid)
            rows.append([uid, _money(spent_u), _money(limit), _usage(spent_u, limit)])
            users_seen.add(uid)
        if rows:
            print()
            _table(["成員", "本隊花費", "本隊上限", "使用率"], rows)

        rows = []
        for k in sorted(keys, key=lambda x: x.get("key_alias") or ""):
            ks = k.get("spend") or 0
            rows.append([k.get("key_alias") or k.get("key_name") or "?", k.get("user_id") or "-",
                         _money(ks), _money(k.get("max_budget")), _usage(ks, k.get("max_budget")),
                         _num(k.get("rpm_limit")), _num(k.get("tpm_limit"))])
        if rows:
            print()
            _table(["key", "擁有者", "花費", "上限", "使用率", "RPM", "TPM"], rows)

    if users_seen:
        print("\n■ 個人限速（跨所有 key 加總）")
        rows = []
        for uid in sorted(users_seen):
            ui = (api.get("/user/info", user_id=uid) or {}).get("user_info") or {}
            rows.append([uid, ui.get("user_email") or "-", _num(ui.get("rpm_limit")), _num(ui.get("tpm_limit"))])
        _table(["人員", "email", "RPM", "TPM"], rows)
    print("\n（花費由 LiteLLM 批次寫入資料庫，可能比實際延遲約 1 分鐘；限制判斷本身是即時的）")


def main():
    ap = argparse.ArgumentParser(description="同步 access/teams.yaml 到 LiteLLM")
    ap.add_argument("command", choices=["validate", "plan", "apply", "report"])
    ap.add_argument("-f", "--file", default=DEFAULT_SPEC, help="設定檔路徑（預設 access/teams.yaml）")
    ap.add_argument("--prune", action="store_true", help="移除設定檔裡已刪掉的團隊成員與本腳本發的 key")
    ap.add_argument("--keys-file", help="apply 時把新 key 以 alias=key 附加寫入此檔（權限 600）")
    ap.add_argument("--team", action="append", help="report 只看指定團隊（可重複）")
    args = ap.parse_args()
    try:
        if args.command == "validate":
            cmd_validate(args)
        elif args.command == "report":
            cmd_report(args)
        else:
            cmd_plan(args, apply=args.command == "apply")
    except SpecError as e:
        sys.exit(f"✘ 設定檔錯誤：{e}")
    except ApiError as e:
        sys.exit(f"✘ LiteLLM API 錯誤：{e}")


if __name__ == "__main__":
    main()
