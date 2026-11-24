#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
odoo_cli.py - a safe, non-destructive Odoo bridge for Hermes Agent.

Zero dependencies (Python 3.8+ stdlib only). Talks to Odoo 14 -> 19+ over
XML-RPC, or over the JSON-2 API (Odoo 19+) when an API key is available.

Safety model ("seatbelts"):
  1. Read-only by default. Writes need ODOO_HERMES_ALLOW_WRITES=1.
  2. Every write is a DRY RUN unless --apply is passed. The dry run shows a
     field-by-field diff of what would change.
  3. Nothing is ever deleted: `unlink` and other destructive methods are
     hard-blocked. "Delete" becomes "archive" (active=False), which is
     reversible.
  4. Every applied change is written to a local journal with the previous
     values, so `undo <entry>` can restore them.
  5. Sensitive models (users, groups, companies, ir.*) and fields
     (passwords, keys, groups, state) are never written.
  6. Mass edits are capped (ODOO_HERMES_MAX_WRITE, default 25 records).
  7. x2many commands that delete records (2) or wipe relations (5), and any
     one2many removal, are refused.

Run `odoo_cli.py --help` or `odoo_cli.py <command> --help`.
"""
from __future__ import annotations

import argparse
import ast
import csv
import difflib
import datetime as dt
import html
import json
import os
import re
import socket
import ssl
import sys
import uuid
import urllib.error
import urllib.parse
import urllib.request
import xmlrpc.client
from collections import Counter, defaultdict
from pathlib import Path

__version__ = "1.0.0"
USER_AGENT = "hermes-odoo-skill/%s" % __version__

# --------------------------------------------------------------------------
# Safety rules
# --------------------------------------------------------------------------

READ_METHODS = {
    "search", "search_read", "read", "search_count", "fields_get",
    "name_search", "name_get", "read_group", "formatted_read_group",
    "default_get", "check_access_rights", "has_access", "context_get",
    "web_search_read", "web_read",
}

# Never callable, whatever the configuration.
HARD_BLOCKED_METHOD_RE = re.compile(
    r"(unlink|uninstall|delete|purge|drop|truncate|wipe|reset|cancel|refund|"
    r"reverse|void|revoke|remove|clear|merge|sudo|^write$|^create$|"
    r"^copy$|action_archive|action_unarchive|toggle_active|execute_sql|"
    r"_auto_init|init$)",
    re.I,
)

PROTECTED_MODEL_RE = re.compile(
    r"^(ir\.|base\.|res\.users|res\.groups|res\.company|res\.config|bus\.|"
    r"auth_|iap\.|mail\.server|fetchmail\.|payment\.provider|"
    r"account\.journal|account\.account|account\.tax)"
)

PROTECTED_FIELD_RE = re.compile(
    r"(password|passwd|api_?key|secret|token|totp|signup_|^groups_id$|"
    r"^group_ids$|^share$)",
    re.I,
)
PROTECTED_FIELDS = {
    "id", "state", "active", "create_uid", "create_date", "write_uid",
    "write_date", "__last_update", "company_id", "company_ids", "move_type",
}

DEFAULT_MAX_WRITE = 25
HARD_MAX_WRITE = 200

# Positional argument order for XML-RPC (JSON-2 always uses names).
POSITIONAL = {
    "search": ["domain"],
    "search_read": ["domain"],
    "search_count": ["domain"],
    "create": ["vals_list"],
    "write": ["vals"],
    "read_group": ["domain", "fields", "groupby"],
    "default_get": ["fields_list"],
}

SMART_FIELDS = [
    "display_name", "partner_id", "email", "phone", "state", "stage_id",
    "date_order", "invoice_date", "invoice_date_due", "date_deadline",
    "amount_total", "amount_residual", "expected_revenue", "list_price",
    "default_code", "user_id", "res_model",
]


class OdooError(Exception):
    """Error returned by Odoo or by the transport."""


class SafetyError(Exception):
    """An operation refused by the skill's safety rules."""


# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------

def _load_env_file():
    """Fallback: pick ODOO_* variables from ~/.hermes/.env if not exported."""
    home = os.environ.get("HERMES_HOME") or os.path.expanduser("~/.hermes")
    path = Path(home) / ".env"
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[7:]
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip().strip('"').strip("'")
        if key.startswith("ODOO_") and key not in os.environ:
            os.environ[key] = value


def _truthy(value):
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


class Config:
    def __init__(self):
        _load_env_file()
        env = os.environ
        self.url = (env.get("ODOO_URL") or "").rstrip("/")
        if self.url and not re.match(r"^https?://", self.url):
            self.url = "https://" + self.url
        self.db = env.get("ODOO_DB") or ""
        self.username = env.get("ODOO_USERNAME") or env.get("ODOO_USER") or ""
        self.api_key = env.get("ODOO_API_KEY") or ""
        self.secret = self.api_key or env.get("ODOO_PASSWORD") or ""
        self.protocol = (env.get("ODOO_PROTOCOL") or "auto").lower()
        self.timeout = float(env.get("ODOO_TIMEOUT") or 30)
        self.verify_ssl = env.get("ODOO_VERIFY_SSL", "1") not in {"0", "false", "no"}
        self.allow_writes = _truthy(env.get("ODOO_HERMES_ALLOW_WRITES"))
        try:
            self.max_write = int(env.get("ODOO_HERMES_MAX_WRITE") or DEFAULT_MAX_WRITE)
        except ValueError:
            self.max_write = DEFAULT_MAX_WRITE
        self.max_write = max(1, min(self.max_write, HARD_MAX_WRITE))
        self.allowed_methods = {
            m.strip() for m in (env.get("ODOO_HERMES_ALLOWED_METHODS") or "").split(",") if m.strip()
        }
        self.extra_protected = {
            m.strip() for m in (env.get("ODOO_HERMES_PROTECTED_MODELS") or "").split(",") if m.strip()
        }
        home = env.get("ODOO_HERMES_HOME") or os.path.join(
            env.get("HERMES_HOME") or os.path.expanduser("~/.hermes"), "odoo")
        self.home = Path(home)

    def missing(self):
        out = []
        if not self.url:
            out.append("ODOO_URL")
        if not self.secret:
            out.append("ODOO_API_KEY (or ODOO_PASSWORD)")
        if self.protocol != "json2" and not self.username:
            out.append("ODOO_USERNAME")
        return out


# --------------------------------------------------------------------------
# Transport / client
# --------------------------------------------------------------------------

class Client:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.url = cfg.url
        self.db = cfg.db
        self.uid = None
        self.protocol = None
        self.version = None
        self._meta_cache = {}
        self._obj = None
        socket.setdefaulttimeout(cfg.timeout)

    # -- helpers ----------------------------------------------------------
    def _ssl_ctx(self):
        if not self.url.startswith("https"):
            return None
        if self.cfg.verify_ssl:
            return ssl.create_default_context()
        return ssl._create_unverified_context()  # explicit user opt-out

    def _proxy(self, endpoint):
        return xmlrpc.client.ServerProxy(
            "%s/xmlrpc/2/%s" % (self.url, endpoint), allow_none=True,
            context=self._ssl_ctx())

    def _http_json(self, path, payload, headers=None):
        data = json.dumps(payload).encode("utf-8")
        hdrs = {"Content-Type": "application/json", "User-Agent": USER_AGENT}
        hdrs.update(headers or {})
        req = urllib.request.Request(self.url + path, data=data, headers=hdrs, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=self.cfg.timeout, context=self._ssl_ctx()) as resp:
                raw = resp.read().decode("utf-8") or "null"
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "replace")
            msg = body
            try:
                err = json.loads(body)
                msg = err.get("message") or err.get("name") or body
            except ValueError:
                pass
            raise OdooError("HTTP %s on %s: %s%s" % (e.code, path, _shorten(msg, 400),
                                                     self._auth_hint() if e.code in (401, 403) else ""))
        except urllib.error.URLError as e:
            raise OdooError("Cannot reach %s: %s" % (self.url, e.reason))
        return json.loads(raw)

    def _jsonrpc(self, path, params):
        res = self._http_json(path, {"jsonrpc": "2.0", "method": "call", "params": params})
        if isinstance(res, dict) and res.get("error"):
            err = res["error"]
            raise OdooError((err.get("data") or {}).get("message") or err.get("message") or str(err))
        return res.get("result") if isinstance(res, dict) else res

    # -- connection -------------------------------------------------------
    def server_version(self):
        if self.version is not None:
            return self.version
        info = None
        try:
            info = self._proxy("common").version()
        except Exception:
            try:
                info = self._jsonrpc("/web/webclient/version_info", {})
            except Exception:
                info = None
        self.version = info or {}
        return self.version

    def major(self):
        raw = str(self.server_version().get("server_version") or "")
        m = re.search(r"(\d+)\.(\d+)", raw)
        return int(m.group(1)) if m else 0

    def _guess_db(self):
        host = urllib.parse.urlsplit(self.url).hostname or ""
        if host.endswith(".odoo.com"):
            return host.split(".")[0]
        for fn in (lambda: self._proxy("db").list(),
                   lambda: self._jsonrpc("/web/database/list", {})):
            try:
                dbs = fn()
                if isinstance(dbs, list) and len(dbs) == 1:
                    return dbs[0]
            except Exception:
                continue
        return ""

    def _auth_hint(self):
        host = urllib.parse.urlsplit(self.url).hostname or ""
        if not host.endswith(".odoo.com"):
            return ""
        return ("\nOdoo Online hints: (1) the external API is only available on the *Custom* pricing plan, "
                "not on Standard or One App Free; (2) API keys expire after at most 3 months - generate a new one "
                "in My Profile > Account Security; (3) use the login of the user who created the key.")

    def connect(self):
        missing = self.cfg.missing()
        if missing:
            raise OdooError("Missing configuration: %s. See the skill's setup section." % ", ".join(missing))
        proto = self.cfg.protocol
        if proto not in {"auto", "xmlrpc", "json2"}:
            raise OdooError("ODOO_PROTOCOL must be auto, xmlrpc or json2")
        if proto == "auto":
            proto = "json2" if (self.major() >= 19 and self.cfg.api_key) else "xmlrpc"
        self.protocol = proto
        if proto == "xmlrpc":
            if not self.db:
                self.db = self._guess_db()
            if not self.db:
                raise OdooError("Cannot guess the database name: set ODOO_DB.")
            try:
                uid = self._proxy("common").authenticate(self.db, self.cfg.username, self.cfg.secret, {})
            except xmlrpc.client.Fault as e:
                raise OdooError("Authentication error: %s" % _fault_msg(e))
            except (OSError, xmlrpc.client.ProtocolError) as e:
                raise OdooError("Cannot reach %s: %s" % (self.url, e))
            if not uid:
                raise OdooError("Authentication failed for %r on database %r.%s" % (
                    self.cfg.username, self.db, self._auth_hint()))
            self.uid = uid
            self._obj = self._proxy("object")
        else:
            self.uid = self._json2_uid()
        return self

    def _json2_uid(self):
        if self.cfg.username:
            try:
                rows = self.call("res.users", "search_read",
                                 domain=[["login", "=", self.cfg.username]], fields=["id"], limit=1)
                if rows:
                    return rows[0]["id"]
            except OdooError:
                pass
        try:
            ctx = self.call("res.users", "context_get")
            if isinstance(ctx, dict) and ctx.get("uid"):
                return ctx["uid"]
        except OdooError as e:
            raise OdooError("JSON-2 authentication failed: %s" % e)
        return None

    # -- RPC --------------------------------------------------------------
    def call(self, model, method, ids=None, context=None, **kw):
        if self.protocol == "json2":
            body = dict(kw)
            if ids is not None:
                body["ids"] = list(ids)
            if context:
                body["context"] = context
            headers = {"Authorization": "bearer %s" % self.cfg.secret}
            if self.db:
                headers["X-Odoo-Database"] = self.db
            return self._http_json("/json/2/%s/%s" % (model, method), body, headers)
        args = [list(ids)] if ids is not None else []
        for name in POSITIONAL.get(method, []):
            if name not in kw:
                break
            args.append(kw.pop(name))
        if context:
            kw["context"] = context
        try:
            return self._obj.execute_kw(self.db, self.uid, self.cfg.secret, model, method, args, kw)
        except xmlrpc.client.Fault as e:
            msg = _fault_msg(e)
            if "cannot marshal None" in msg:
                return None  # method ran, but returned None
            raise OdooError(msg)
        except (OSError, xmlrpc.client.ProtocolError) as e:
            raise OdooError("Transport error: %s" % e)

    # -- conveniences -----------------------------------------------------
    def fields(self, model):
        if model not in self._meta_cache:
            self._meta_cache[model] = self.call(
                model, "fields_get",
                attributes=["string", "type", "required", "readonly", "relation", "selection", "store"])
        return self._meta_cache[model]

    def search_read(self, model, domain, fields, limit=None, offset=0, order=None, context=None):
        kw = {"domain": domain, "fields": fields}
        if limit:
            kw["limit"] = limit
        if offset:
            kw["offset"] = offset
        if order:
            kw["order"] = order
        return self.call(model, "search_read", context=context, **kw)

    def count(self, model, domain, context=None):
        return self.call(model, "search_count", context=context, domain=domain)

    def read(self, model, ids, fields, context=None):
        return self.call(model, "read", ids=ids, context=context or {"active_test": False}, fields=fields)

    def link(self, model, rec_id):
        return "%s/web#id=%s&model=%s&view_type=form" % (self.url, rec_id, model)

    def has_model(self, model):
        try:
            self.fields(model)
            return True
        except OdooError:
            return False


def _fault_msg(fault):
    text = str(getattr(fault, "faultString", fault) or "")
    lines = [l.strip() for l in text.strip().splitlines() if l.strip()]
    return _shorten(lines[-1] if lines else text, 500)


def _shorten(text, n):
    text = str(text)
    return text if len(text) <= n else text[: n - 1] + "…"


# --------------------------------------------------------------------------
# Mini query language:  "name ~ acme, customer_rank > 0, !email"
# --------------------------------------------------------------------------

SYMBOL_OPS = {"!~": "not ilike", "~": "ilike", ">=": ">=", "<=": "<=", "!=": "!=",
              "==": "=", "=": "=", ">": ">", "<": "<"}
WORD_RE = re.compile(r"^\s*([\w.]+)\s+(not\s+in|in|child_of|parent_of|like|ilike|not\s+ilike|=like|=ilike)\s+(.+)$", re.I)
SYM_RE = re.compile(r"^\s*([\w.]+)\s*(!~|>=|<=|!=|==|=|>|<|~)\s*(.*)$")


def split_top(text, sep=","):
    parts, buf, depth, quote = [], [], 0, None
    for ch in text:
        if quote:
            buf.append(ch)
            if ch == quote:
                quote = None
            continue
        if ch in "\"'":
            quote = ch
        elif ch in "[(":
            depth += 1
        elif ch in "])":
            depth -= 1
        elif ch == sep and depth == 0:
            parts.append("".join(buf))
            buf = []
            continue
        buf.append(ch)
    if "".join(buf).strip():
        parts.append("".join(buf))
    return [p.strip() for p in parts if p.strip()]


def expand_macro(token, client=None):
    """@today, @today-7, @today+30, @now, @week_start, @month_start, @year_start, @me"""
    m = re.fullmatch(r"@(today|now|week_start|month_start|year_start|me)(?:([+-]\d+)([dwmy]?))?", token.strip())
    if not m:
        return token
    name, delta, unit = m.group(1), m.group(2), m.group(3) or "d"
    if name == "me":
        return client.uid if client else token
    today = dt.date.today()
    base = {
        "today": today,
        "week_start": today - dt.timedelta(days=today.weekday()),
        "month_start": today.replace(day=1),
        "year_start": today.replace(month=1, day=1),
    }.get(name)
    if delta:
        n = int(delta)
        if unit == "w":
            n *= 7
        if unit in "dw":
            if name == "now":
                return (dt.datetime.utcnow() + dt.timedelta(days=n)).strftime("%Y-%m-%d %H:%M:%S")
            base = base + dt.timedelta(days=n)
        else:
            months = n * (12 if unit == "y" else 1)
            b = base or today
            y, mo = divmod(b.month - 1 + months, 12)
            base = b.replace(year=b.year + y, month=mo + 1, day=min(b.day, 28))
    if name == "now" and base is None:
        return dt.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    return base.isoformat()


def parse_value(raw, client=None):
    s = raw.strip()
    if s.startswith("@"):
        return expand_macro(s, client)
    low = s.lower()
    if low in {"true", "false"}:
        return low == "true"
    if low in {"null", "none"}:
        return False
    for loader in (json.loads, ast.literal_eval):
        try:
            return loader(s)
        except (ValueError, SyntaxError, TypeError):
            continue
    return s


def parse_where(text, client=None):
    """Turn the mini-DSL (or a raw JSON domain) into an Odoo domain."""
    if text is None:
        return []
    text = text.strip()
    if not text or text == "*":
        return []
    if text.startswith("["):
        try:
            return json.loads(text)
        except ValueError:
            return ast.literal_eval(text)
    domain = []
    for clause in split_top(text):
        if re.fullmatch(r"!\s*[\w.]+", clause):
            domain.append([clause[1:].strip(), "=", False])
            continue
        if re.fullmatch(r"[\w.]+", clause):
            domain.append([clause, "!=", False])
            continue
        m = WORD_RE.match(clause)
        if m:
            op = re.sub(r"\s+", " ", m.group(2).lower())
            domain.append([m.group(1), op, parse_value(m.group(3), client)])
            continue
        m = SYM_RE.match(clause)
        if m:
            domain.append([m.group(1), SYMBOL_OPS[m.group(2)], parse_value(m.group(3), client)])
            continue
        raise SafetyError("Cannot parse condition %r. Expected e.g. `name ~ acme` or `amount_total > 1000`." % clause)
    return domain


# --------------------------------------------------------------------------
# Formatting
# --------------------------------------------------------------------------

TAG_RE = re.compile(r"<[^>]+>")


def strip_html(text):
    return re.sub(r"\s+", " ", html.unescape(TAG_RE.sub(" ", text or ""))).strip()


def fmt(v, width=48):
    if v is False or v is None:
        s = ""
    elif v is True:
        s = "yes"
    elif isinstance(v, list):
        if len(v) == 2 and isinstance(v[0], int) and isinstance(v[1], str):
            s = "%s #%s" % (v[1], v[0])
        elif not v:
            s = ""
        elif all(isinstance(x, int) for x in v):
            s = ",".join(str(x) for x in v[:6]) + ("…(+%d)" % (len(v) - 6) if len(v) > 6 else "")
            s = "[%s]" % s
        else:
            s = json.dumps(v, ensure_ascii=False, default=str)
    elif isinstance(v, float):
        s = "{:,.2f}".format(v)
    elif isinstance(v, dict):
        s = json.dumps(v, ensure_ascii=False, default=str)
    else:
        s = str(v)
        if "<" in s and ">" in s:
            s = strip_html(s)
        s = s.replace("\n", " ")
    return _shorten(s, width) if width else s


def table(rows, cols, width=40):
    if not rows:
        return "(no records)"
    cells = [[fmt(r.get(c), width) for c in cols] for r in rows]
    widths = [max(len(c), *(len(row[i]) for row in cells)) for i, c in enumerate(cols)]
    line = lambda vals: " | ".join(v.ljust(widths[i]) for i, v in enumerate(vals)).rstrip()
    out = [line(cols), "-+-".join("-" * w for w in widths)]
    out += [line(row) for row in cells]
    return "\n".join(out)


def money(amount, currency=""):
    return ("{:,.2f} {}".format(amount or 0.0, currency)).strip()


def emit(args, data, text):
    if getattr(args, "json", False):
        print(json.dumps(data, ensure_ascii=False, indent=2, default=str))
    else:
        print(text() if callable(text) else text)


# --------------------------------------------------------------------------
# Journal (append-only, local) - enables `undo`
# --------------------------------------------------------------------------

class Journal:
    def __init__(self, cfg):
        self.path = cfg.home / "journal.jsonl"

    def append(self, entry):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        entry = dict(entry)
        entry.setdefault("id", uuid.uuid4().hex[:8])
        entry.setdefault("ts", dt.datetime.now().isoformat(timespec="seconds"))
        new = not self.path.exists()
        with open(self.path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")
        if new:
            try:
                os.chmod(self.path, 0o600)
            except OSError:
                pass
        return entry["id"]

    def entries(self):
        if not self.path.exists():
            return []
        out = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            try:
                out.append(json.loads(line))
            except ValueError:
                continue
        return out

    def get(self, entry_id):
        matches = [e for e in self.entries() if e.get("id", "").startswith(entry_id)]
        if not matches:
            raise SafetyError("No journal entry %r." % entry_id)
        if len(matches) > 1:
            raise SafetyError("Ambiguous journal id %r." % entry_id)
        return matches[0]

    def undone(self, entry_id):
        return any(e.get("undo_of") == entry_id for e in self.entries())


# --------------------------------------------------------------------------
# Write guards and value preparation
# --------------------------------------------------------------------------

def check_model_writable(cfg, model):
    if PROTECTED_MODEL_RE.match(model) or model in cfg.extra_protected:
        raise SafetyError("Model %s is protected: the skill never writes to it." % model)


def check_fields_writable(fields):
    for f in fields:
        if f in PROTECTED_FIELDS or PROTECTED_FIELD_RE.search(f):
            hint = ""
            if f == "active":
                hint = " Use `archive` / `unarchive` instead."
            elif f == "state":
                hint = " Use the business action (e.g. `call ... action_confirm`) instead."
            raise SafetyError("Field %r is protected and cannot be written.%s" % (f, hint))


def check_apply_allowed(cfg):
    if not cfg.allow_writes:
        raise SafetyError(
            "Read-only mode: writes are disabled. The owner must set "
            "ODOO_HERMES_ALLOW_WRITES=1 to allow (still journaled and undoable) changes.")


def resolve_name(client, relation, text):
    text = text.strip()
    if re.fullmatch(r"#?\d+", text):
        return int(text.lstrip("#"))
    for op in ("=", "ilike"):
        try:
            res = client.call(relation, "name_search", name=text, operator=op, limit=6)
        except OdooError:
            res = []
        if len(res) == 1:
            return res[0][0]
        if len(res) > 1 and op == "ilike":
            cands = "; ".join("%s #%s" % (r[1], r[0]) for r in res)
            raise SafetyError("'@%s' is ambiguous on %s: %s. Use an id instead." % (text, relation, cands))
    raise SafetyError("No %s record matches '@%s'." % (relation, text))


def sanitize_x2many(client, field, ftype, relation, value):
    if not isinstance(value, list):
        raise SafetyError("Field %s (%s) expects a list." % (field, ftype))
    cmds = []
    for item in value:
        if isinstance(item, bool):
            raise SafetyError("Invalid value in %s." % field)
        if isinstance(item, int):
            cmds.append([4, item, 0])
        elif isinstance(item, str):
            cmds.append([4, resolve_name(client, relation, item.lstrip("@")), 0])
        elif isinstance(item, dict):
            cmds.append([0, 0, item])
        elif isinstance(item, (list, tuple)) and item and isinstance(item[0], int):
            code = item[0]
            if code in (2, 5):
                raise SafetyError("x2many command %d on %s is destructive and refused." % (code, field))
            if ftype == "one2many" and code not in (0, 1, 4):
                raise SafetyError("Removing lines from one2many %s may delete them: refused." % field)
            cmds.append(list(item))
        else:
            raise SafetyError("Unsupported value %r in %s." % (item, field))
    return cmds


def prepare_vals(client, model, vals):
    meta = client.fields(model)
    out, notes = {}, []
    for key, value in vals.items():
        if key not in meta:
            close = difflib.get_close_matches(key, list(meta), n=4, cutoff=0.6) or \
                [f for f in meta if key.lower() in f.lower()][:4]
            raise SafetyError("Unknown field %r on %s.%s" % (
                key, model, (" Did you mean: %s?" % ", ".join(close)) if close else ""))
        f = meta[key]
        ftype = f.get("type")
        if f.get("readonly"):
            notes.append("warning: %s is readonly in the UI, Odoo may ignore or reject it" % key)
        if ftype == "many2one":
            if isinstance(value, str) and value.startswith("@"):
                rid = resolve_name(client, f["relation"], value[1:])
                notes.append("%s: '%s' resolved to %s #%s" % (key, value, f["relation"], rid))
                value = rid
            elif isinstance(value, list) and len(value) == 2 and isinstance(value[0], int):
                value = value[0]
        elif ftype in ("many2many", "one2many"):
            value = sanitize_x2many(client, key, ftype, f.get("relation"), value)
        elif ftype in ("date", "datetime") and isinstance(value, str) and value.startswith("@"):
            value = expand_macro(value, client)
            if ftype == "datetime" and len(value) == 10:
                value += " 00:00:00"
        elif ftype in ("char", "text", "html") and isinstance(value, (int, float)) and not isinstance(value, bool):
            value = str(value)
        elif ftype == "selection" and isinstance(f.get("selection"), list):
            keys = [s[0] for s in f["selection"]]
            if value not in keys and str(value) in keys:
                value = str(value)
            if keys and value not in keys and value is not False:
                raise SafetyError("%s must be one of %s" % (key, keys))
        out[key] = value
    return out, notes


def norm(v):
    if v is None:
        return False
    if isinstance(v, (list, tuple)):
        if len(v) == 2 and isinstance(v[0], int) and isinstance(v[1], str):
            return v[0]
        if all(isinstance(x, int) and not isinstance(x, bool) for x in v):
            return sorted(v)
    return v


def restore_value(value, ftype):
    if ftype == "many2one":
        return norm(value) or False
    if ftype == "many2many":
        return [[6, 0, norm(value) or []]]
    return value


def gather_vals(args):
    vals = {}
    if getattr(args, "values", None):
        try:
            parsed = json.loads(args.values)
        except ValueError as e:
            raise SafetyError("--values must be JSON: %s" % e)
        if isinstance(parsed, dict):
            vals.update(parsed)
        else:
            raise SafetyError("--values must be a JSON object here.")
    for pair in getattr(args, "set", None) or []:
        if "=" not in pair:
            raise SafetyError("--set expects key=value, got %r" % pair)
        k, v = pair.split("=", 1)
        v = v.strip()
        if v.startswith("@"):
            vals[k.strip()] = v  # name/macro resolution happens in prepare_vals
        else:
            vals[k.strip()] = parse_value(v)
    return vals


def target_ids(client, cfg, model, args):
    if getattr(args, "ids", None):
        ids = [int(x) for x in re.split(r"[,\s]+", str(args.ids).strip()) if x]
    elif getattr(args, "where", None):
        domain = parse_where(args.where, client)
        if not domain:
            raise SafetyError("Refusing an empty --where (it would target every record).")
        ids = client.call(model, "search", domain=domain, limit=cfg.max_write + 1,
                          context={"active_test": not getattr(args, "archived", False)})
    else:
        raise SafetyError("Give --ids or --where.")
    if not ids:
        raise SafetyError("No matching records.")
    if len(ids) > cfg.max_write:
        raise SafetyError(
            "That would touch more than %d records (ODOO_HERMES_MAX_WRITE). Narrow the selection "
            "or work in batches." % cfg.max_write)
    return ids


DRY = "\nDRY RUN - nothing was changed. Show this preview to the user and re-run with --apply once they confirm."


# --------------------------------------------------------------------------
# Read commands
# --------------------------------------------------------------------------

def cmd_doctor(client, cfg, args):
    report = {"url": cfg.url, "protocol_setting": cfg.protocol}
    lines = ["Odoo doctor", "==========="]
    v = client.server_version()
    report["server_version"] = v.get("server_version")
    lines.append("server   : %s  (%s)" % (cfg.url, v.get("server_version") or "version unknown"))
    client.connect()
    report.update({"protocol": client.protocol, "db": client.db, "uid": client.uid})
    lines.append("protocol : %s%s" % (client.protocol, "  (XML-RPC is deprecated from Odoo 20: prefer an API key + JSON-2)"
                                       if client.protocol == "xmlrpc" and client.major() >= 19 else ""))
    lines.append("database : %s" % (client.db or "(from server)"))
    try:
        me = client.read("res.users", [client.uid], ["name", "login", "company_id", "tz", "lang"])[0] if client.uid else {}
    except OdooError:
        me = {}
    report["user"] = me
    lines.append("user     : %s <%s> uid=%s" % (me.get("name", "?"), me.get("login", "?"), client.uid))
    lines.append("company  : %s" % fmt(me.get("company_id")))
    try:
        mods = client.search_read("ir.module.module", [["state", "=", "installed"], ["application", "=", True]],
                                  ["name", "shortdesc"], order="name")
        report["apps"] = [m["name"] for m in mods]
        lines.append("apps     : %s" % ", ".join(m["name"] for m in mods))
    except OdooError:
        lines.append("apps     : (not readable with this user)")
    lines.append("writes   : %s" % ("ENABLED (dry-run first, --apply to execute, journaled, max %d records)" % cfg.max_write
                                   if cfg.allow_writes else "disabled (read-only mode)"))
    lines.append("journal  : %s" % (cfg.home / "journal.jsonl"))
    report["writes_enabled"] = cfg.allow_writes
    emit(args, report, "\n".join(lines))


def cmd_whoami(client, cfg, args):
    me = client.read("res.users", [client.uid], ["name", "login", "email", "company_id", "tz", "lang"])[0]
    emit(args, me, lambda: "\n".join("%-8s: %s" % (k, fmt(v)) for k, v in me.items()))


def cmd_models(client, cfg, args):
    q = args.query or ""
    domain = ["|", ["model", "ilike", q], ["name", "ilike", q]] if q else []
    rows = client.search_read("ir.model", domain, ["model", "name"], limit=args.limit, order="model")
    emit(args, rows, lambda: table(rows, ["model", "name"]))


def cmd_fields(client, cfg, args):
    meta = client.fields(args.model)
    rows = []
    for name, f in meta.items():
        if args.filter and args.filter.lower() not in (name + " " + str(f.get("string"))).lower():
            continue
        if args.required and not f.get("required"):
            continue
        rows.append({"field": name, "type": f.get("type"), "relation": f.get("relation") or "",
                     "req": "R" if f.get("required") else "", "ro": "ro" if f.get("readonly") else "",
                     "label": f.get("string")})
    rows.sort(key=lambda r: (r["req"] != "R", r["field"]))
    emit(args, rows, lambda: table(rows, ["field", "type", "relation", "req", "ro", "label"]))


def smart_fields(meta, wanted=None):
    if wanted:
        fields = [f.strip() for f in wanted.split(",") if f.strip()]
        unknown = [f for f in fields if f not in meta]
        if unknown:
            raise SafetyError("Unknown field(s) %s. Try `fields <model> --filter ...`." % ", ".join(unknown))
        return fields
    picked = [f for f in SMART_FIELDS if f in meta][:6]
    if not picked and "name" in meta:
        picked = ["name"]
    return picked


def cmd_find(client, cfg, args):
    meta = client.fields(args.model)
    fields = smart_fields(meta, args.fields)
    domain = parse_where(args.where, client)
    ctx = {"active_test": False} if args.archived else None
    limit = min(args.limit, 500)
    rows = client.search_read(args.model, domain, fields, limit=limit, offset=args.offset,
                              order=args.order, context=ctx)
    total = client.count(args.model, domain, context=ctx)
    data = {"model": args.model, "domain": domain, "total": total, "records": rows}

    def text():
        out = [table(rows, ["id"] + [f for f in fields if f != "id"])]
        out.append("\n%d shown / %d matching  domain=%s" % (len(rows), total, json.dumps(domain, default=str)))
        if total > len(rows) + args.offset:
            out.append("(more available: use --offset %d or refine the query)" % (args.offset + len(rows)))
        return "\n".join(out)
    emit(args, data, text)


def cmd_get(client, cfg, args):
    meta = client.fields(args.model)
    ids = [int(i) for i in args.ids]
    if args.fields == "all":
        fields = [f for f, m in meta.items() if m.get("type") != "binary"]
    elif args.fields:
        fields = smart_fields(meta, args.fields)
    else:
        fields = [f for f, m in meta.items()
                  if m.get("type") not in ("binary", "one2many") and m.get("store", True)
                  and not f.startswith(("message_", "activity_", "website_message"))]
    recs = client.read(args.model, ids, fields)
    for r in recs:
        r["_link"] = client.link(args.model, r["id"])

    def text():
        blocks = []
        for r in recs:
            lines = ["%s #%s  %s" % (args.model, r["id"], r["_link"])]
            for f in fields:
                v = r.get(f)
                if (v in (False, None, [], "")) and not args.show_empty:
                    continue
                lines.append("  %-28s %s" % (f, fmt(v, 120)))
            blocks.append("\n".join(lines))
        return "\n\n".join(blocks) or "(not found)"
    emit(args, recs, text)


def cmd_count(client, cfg, args):
    domain = parse_where(args.where, client)
    n = client.count(args.model, domain)
    emit(args, {"model": args.model, "domain": domain, "count": n}, "%d" % n)


def cmd_group(client, cfg, args):
    meta = client.fields(args.model)
    domain = parse_where(args.where, client)
    by = [b.strip() for b in args.by.split(",") if b.strip()]
    sums = [s.strip() for s in (args.sum or "").split(",") if s.strip()]
    for f in by + sums:
        if f.split(":")[0] not in meta:
            raise SafetyError("Unknown field %r on %s." % (f, args.model))
    rows = []
    try:
        res = client.call(args.model, "read_group", domain=domain, fields=sums + [by[0].split(":")[0]],
                          groupby=by, lazy=False, orderby=args.order or False)
        for g in res:
            row = {b: g.get(b) for b in by}
            row["count"] = g.get("__count", g.get("%s_count" % by[0], 0))
            for s in sums:
                row[s] = g.get(s)
            rows.append(row)
    except OdooError:
        # Fallback: aggregate client-side (read_group changed in recent versions).
        recs = client.search_read(args.model, domain, [b.split(":")[0] for b in by] + sums, limit=20000)
        agg = {}
        for r in recs:
            vals = {b: _bucket(r.get(b.split(":")[0]), b) for b in by}
            key = tuple(json.dumps(vals[b], default=str) for b in by)
            row = agg.setdefault(key, dict(vals, count=0, **{s: 0.0 for s in sums}))
            row["count"] += 1
            for s in sums:
                row[s] += r.get(s) or 0
        rows = sorted(agg.values(), key=lambda r: -r["count"])
    emit(args, rows, lambda: table(rows, by + ["count"] + sums))


def _bucket(value, spec):
    """Client-side date bucketing for `group --by date:month` fallbacks."""
    if not isinstance(value, str) or ":" not in spec:
        return value
    unit = spec.split(":", 1)[1]
    if unit == "year":
        return value[:4]
    if unit == "month":
        return value[:7]
    if unit == "week":
        try:
            y, w, _ = dt.date.fromisoformat(value[:10]).isocalendar()
            return "%d-W%02d" % (y, w)
        except ValueError:
            return value
    return value[:10]


def cmd_lookup(client, cfg, args):
    models = [m.strip() for m in (args.models or "").split(",") if m.strip()] or [
        "res.partner", "product.template", "sale.order", "account.move", "crm.lead",
        "purchase.order", "project.project", "project.task", "stock.picking",
        "hr.employee", "helpdesk.ticket"]
    found = {}
    for model in models:
        try:
            res = client.call(model, "name_search", name=args.text, limit=args.limit)
        except OdooError:
            continue
        if res:
            found[model] = [{"id": r[0], "name": r[1], "link": client.link(model, r[0])} for r in res]

    def text():
        if not found:
            return "Nothing found for %r." % args.text
        out = []
        for model, recs in found.items():
            out.append("%s" % model)
            out += ["  #%-6s %s" % (r["id"], r["name"]) for r in recs]
        return "\n".join(out)
    emit(args, found, text)


def _sum_by_currency(rows, amount_field):
    totals = defaultdict(float)
    for r in rows:
        cur = r.get("currency_id")
        totals[cur[1] if isinstance(cur, list) else ""] += r.get(amount_field) or 0.0
    return " + ".join(money(v, k) for k, v in totals.items()) or money(0)


def cmd_pulse(client, cfg, args):
    """A business heartbeat: only modules that exist are queried."""
    today = dt.date.today().isoformat()
    soon = (dt.date.today() + dt.timedelta(days=7)).isoformat()
    month = dt.date.today().replace(day=1).isoformat()
    now = dt.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    sections = []

    def metric(section, label, fn):
        try:
            value = fn()
        except OdooError:
            return
        sections.append({"section": section, "metric": label, "value": value})

    if client.has_model("sale.order"):
        def sales():
            rows = client.search_read("sale.order", [["state", "in", ["sale", "done"]], ["date_order", ">=", month]],
                                      ["amount_total", "currency_id"], limit=10000)
            return "%d orders, %s" % (len(rows), _sum_by_currency(rows, "amount_total"))
        metric("Sales", "confirmed this month", sales)
        metric("Sales", "open quotations", lambda: client.count("sale.order", [["state", "in", ["draft", "sent"]]]))
    if client.has_model("account.move"):
        def overdue(kind, cmp, date):
            def fn():
                rows = client.search_read("account.move", [
                    ["move_type", "=", kind], ["state", "=", "posted"],
                    ["payment_state", "in", ["not_paid", "partial"]], ["invoice_date_due", cmp, date]],
                    ["amount_residual", "currency_id"], limit=10000)
                return "%d, %s" % (len(rows), _sum_by_currency(rows, "amount_residual"))
            return fn
        metric("Invoicing", "overdue customer invoices", overdue("out_invoice", "<", today))
        metric("Invoicing", "vendor bills due within 7 days", overdue("in_invoice", "<=", soon))
    if client.has_model("crm.lead"):
        def pipe():
            rows = client.search_read("crm.lead", [["type", "=", "opportunity"]],
                                      ["expected_revenue", "company_currency"] if "company_currency" in client.fields("crm.lead")
                                      else ["expected_revenue"], limit=10000)
            return "%d opportunities, %s expected" % (len(rows), money(sum(r.get("expected_revenue") or 0 for r in rows)))
        metric("CRM", "open pipeline", pipe)
        metric("CRM", "won this month", lambda: client.count("crm.lead", [["stage_id.is_won", "=", True], ["date_closed", ">=", month]]))
    if client.has_model("purchase.order"):
        metric("Purchase", "RFQs waiting", lambda: client.count("purchase.order", [["state", "in", ["draft", "sent", "to approve"]]]))
    if client.has_model("stock.picking"):
        metric("Inventory", "late transfers", lambda: client.count("stock.picking", [
            ["state", "in", ["assigned", "confirmed", "waiting"]], ["scheduled_date", "<", now]]))
    if client.has_model("project.task"):
        metric("Project", "overdue tasks", lambda: client.count("project.task", [
            ["date_deadline", "<", today], ["stage_id.fold", "=", False]]))
    if client.has_model("helpdesk.ticket"):
        metric("Helpdesk", "open tickets", lambda: client.count("helpdesk.ticket", [["stage_id.fold", "=", False]]))
    if client.has_model("mail.activity") and client.uid:
        metric("Me", "my activities due today or late", lambda: client.count("mail.activity", [
            ["user_id", "=", client.uid], ["date_deadline", "<=", today]]))

    def text():
        if not sections:
            return "No business app readable with this user."
        out, last = ["Odoo pulse - %s" % today], None
        for s in sections:
            if s["section"] != last:
                out.append("\n[%s]" % s["section"])
                last = s["section"]
            out.append("  %-34s %s" % (s["metric"], s["value"]))
        return "\n".join(out)
    emit(args, sections, text)


def cmd_todo(client, cfg, args):
    horizon = (dt.date.today() + dt.timedelta(days=args.days)).isoformat()
    domain = [["date_deadline", "<=", horizon]]
    if not args.everyone:
        domain.append(["user_id", "=", client.uid])
    rows = client.search_read("mail.activity", domain,
                              ["res_model", "res_id", "res_name", "summary", "activity_type_id", "date_deadline", "user_id"],
                              limit=args.limit, order="date_deadline asc")
    today = dt.date.today().isoformat()
    for r in rows:
        d = str(r.get("date_deadline"))
        r["when"] = "LATE" if d < today else ("today" if d == today else d)
        r["record"] = "%s %s #%s" % (r.get("res_model"), r.get("res_name") or "", r.get("res_id"))
    emit(args, rows, lambda: table(rows, ["when", "activity_type_id", "summary", "record", "user_id"]))


def cmd_history(client, cfg, args):
    rows = client.search_read("mail.message", [["model", "=", args.model], ["res_id", "=", int(args.id)]],
                              ["date", "author_id", "message_type", "subtype_id", "body", "tracking_value_ids"],
                              limit=args.limit, order="date desc, id desc")
    tracking = {}
    tv_ids = [t for r in rows for t in (r.get("tracking_value_ids") or [])]
    if tv_ids:
        for fields in (["field_id", "old_value_char", "new_value_char", "old_value_float", "new_value_float"],
                       ["field", "old_value_char", "new_value_char"]):
            try:
                for t in client.read("mail.tracking.value", tv_ids, fields):
                    tracking[t["id"]] = t
                break
            except OdooError:
                continue
    for r in rows:
        r["body"] = strip_html(r.get("body"))
        changes = []
        for t in r.get("tracking_value_ids") or []:
            tv = tracking.get(t)
            if tv:
                fname = fmt(tv.get("field_id") or tv.get("field"))
                old = tv.get("old_value_char") or tv.get("old_value_float") or ""
                new = tv.get("new_value_char") or tv.get("new_value_float") or ""
                changes.append("%s: %s -> %s" % (fname, old, new))
        r["changes"] = changes

    def text():
        out = []
        for r in rows:
            out.append("%s  %s  [%s]" % (r.get("date"), fmt(r.get("author_id")), fmt(r.get("subtype_id")) or r.get("message_type")))
            if r["body"]:
                out.append("    " + _shorten(r["body"], 300))
            for c in r["changes"]:
                out.append("    * " + c)
        return "\n".join(out) or "(no chatter)"
    emit(args, rows, text)


def cmd_360(client, cfg, args):
    """Customer 360: everything about one partner, on one screen."""
    if re.fullmatch(r"#?\d+", args.partner):
        pid = int(args.partner.lstrip("#"))
    else:
        pid = resolve_name(client, "res.partner", args.partner)
    pmeta = client.fields("res.partner")
    want = [f for f in ["display_name", "email", "phone", "mobile", "street", "city", "country_id", "vat",
                        "user_id", "category_id", "customer_rank", "supplier_rank", "credit", "debit"] if f in pmeta]
    partner = client.read("res.partner", [pid], want)[0]
    child = [["partner_id", "child_of", pid]]
    out = {"partner": partner, "link": client.link("res.partner", pid)}

    def section(key, model, domain, fields, order, limit=5):
        if not client.has_model(model):
            return
        meta = client.fields(model)
        try:
            out[key] = client.search_read(model, domain, [f for f in fields if f in meta], limit=limit, order=order)
        except OdooError:
            pass

    section("sales", "sale.order", child, ["name", "date_order", "state", "amount_total"], "date_order desc")
    section("unpaid_invoices", "account.move", child + [["move_type", "in", ["out_invoice", "out_refund"]],
            ["state", "=", "posted"], ["payment_state", "in", ["not_paid", "partial"]]],
            ["name", "invoice_date_due", "amount_residual", "payment_state"], "invoice_date_due asc", 10)
    section("opportunities", "crm.lead", child, ["name", "stage_id", "expected_revenue", "probability"], "id desc")
    section("tickets", "helpdesk.ticket", child, ["name", "stage_id", "priority"], "id desc")
    section("activities", "mail.activity", [["res_model", "=", "res.partner"], ["res_id", "=", pid]],
            ["summary", "activity_type_id", "date_deadline", "user_id"], "date_deadline asc")
    section("recent_messages", "mail.message", [["model", "=", "res.partner"], ["res_id", "=", pid],
            ["message_type", "in", ["comment", "email"]]], ["date", "author_id", "body"], "date desc", 3)

    def text():
        lines = ["%s  (#%s)  %s" % (partner.get("display_name"), pid, out["link"])]
        for k in want[1:]:
            if partner.get(k) not in (False, None, [], ""):
                lines.append("  %-14s %s" % (k, fmt(partner[k], 80)))
        titles = {"sales": "Last sales orders", "unpaid_invoices": "Unpaid invoices", "opportunities": "Opportunities",
                  "tickets": "Tickets", "activities": "Planned activities", "recent_messages": "Recent messages"}
        for key, title in titles.items():
            if key in out:
                rows = out[key]
                lines.append("\n[%s] %s" % (title, "" if rows else "none"))
                if rows:
                    cols = [c for c in rows[0].keys() if c != "id"]
                    lines.append(table(rows, ["id"] + cols, 36))
        if out.get("unpaid_invoices"):
            due = sum(r.get("amount_residual") or 0 for r in out["unpaid_invoices"])
            lines.append("\nOutstanding: %s" % money(due))
        return "\n".join(lines)
    emit(args, out, text)


def cmd_hygiene(client, cfg, args):
    """Read-only data quality audit of contacts. Suggests, never fixes."""
    meta = client.fields("res.partner")
    base = [["type", "=", "contact"]] if "type" in meta else []
    report = {"total": client.count("res.partner", base)}
    checks = [("missing_email", [["email", "=", False]]), ("missing_phone", [["phone", "=", False]])]
    if "vat" in meta and "is_company" in meta:
        checks.append(("companies_without_vat", [["is_company", "=", True], ["vat", "=", False]]))
    if "country_id" in meta:
        checks.append(("missing_country", [["country_id", "=", False]]))
    for key, dom in checks:
        try:
            report[key] = client.count("res.partner", base + dom)
        except OdooError:
            pass
    recs = client.search_read("res.partner", base + [["email", "!=", False]], ["email"], limit=args.scan)
    emails = Counter(r["email"].strip().lower() for r in recs if r.get("email"))
    report["duplicate_emails"] = [{"email": e, "count": n} for e, n in emails.most_common() if n > 1][:args.top]
    names_rows = client.search_read("res.partner", base + ([["is_company", "=", True]] if "is_company" in meta else []),
                                    ["name"], limit=args.scan)
    names = Counter(re.sub(r"[^a-z0-9]", "", (r.get("name") or "").lower()) for r in names_rows)
    report["duplicate_company_names"] = [{"key": k, "count": n} for k, n in names.most_common() if n > 1 and k][:args.top]
    bad = [r for r in recs if r.get("email") and not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", r["email"].strip())]
    report["malformed_emails"] = [{"id": r["id"], "email": r["email"]} for r in bad[:args.top]]

    def text():
        lines = ["Contact hygiene audit (read-only)", "contacts scanned: %d" % report["total"]]
        for key, _ in checks:
            if key in report:
                lines.append("  %-24s %d" % (key.replace("_", " "), report[key]))
        if report["duplicate_emails"]:
            lines.append("\nPossible duplicates by email:")
            lines += ["  %s  x%d   -> find res.partner \"email =ilike %s\"" % (d["email"], d["count"], d["email"])
                      for d in report["duplicate_emails"]]
        if report["duplicate_company_names"]:
            lines.append("\nCompanies with near-identical names:")
            lines += ["  %s  x%d" % (d["key"], d["count"]) for d in report["duplicate_company_names"]]
        if report["malformed_emails"]:
            lines.append("\nMalformed emails:")
            lines += ["  #%s %s" % (d["id"], d["email"]) for d in report["malformed_emails"]]
        lines.append("\nNothing was modified. Merging duplicates must be done by a human in Odoo (Contacts > Merge).")
        return "\n".join(lines)
    emit(args, report, text)


def cmd_export(client, cfg, args):
    meta = client.fields(args.model)
    fields = smart_fields(meta, args.fields)
    domain = parse_where(args.where, client)
    rows = client.search_read(args.model, domain, ["id"] + [f for f in fields if f != "id"],
                              limit=min(args.limit, 50000), order=args.order)
    out = Path(args.out or "odoo_%s_%s.csv" % (args.model.replace(".", "_"), dt.datetime.now().strftime("%Y%m%d_%H%M%S")))
    if out.exists() and not args.overwrite:
        raise SafetyError("%s already exists (use --overwrite or another --out)." % out)
    with open(out, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        cols = ["id"] + [f for f in fields if f != "id"]
        w.writerow(cols)
        for r in rows:
            w.writerow([fmt(r.get(c), 0) for c in cols])
    emit(args, {"file": str(out.resolve()), "rows": len(rows)}, "Exported %d rows to %s" % (len(rows), out.resolve()))


# --------------------------------------------------------------------------
# Write commands (dry-run by default, journaled, undoable)
# --------------------------------------------------------------------------

def _names(client, model, ids):
    try:
        return {r["id"]: r.get("display_name") for r in client.read(model, ids, ["display_name"])}
    except OdooError:
        return {i: "" for i in ids}


# "MadeByHermes" marker: every record created by the CLI is tagged, in the
# most relevant place and NEVER in a field printed for the customer.
# Re-read in main() after Config() has loaded ~/.hermes/.env.
HERMES_MARKER = os.environ.get("ODOO_HERMES_MARKER", "MadeByHermes")

# Internal-note fields, checked on a real Odoo 19.0+e instance.
MARKER_FIELDS = {
    "res.partner": "comment", "crm.lead": "description",
    "project.project": "description", "project.task": "description",
    "product.template": "description", "product.product": "description",
    "hr.employee": "additional_note", "mail.activity": "note",
    "planning.slot": "name", "note.note": "memo",
}
MARKER_FALLBACK_FIELDS = ("note", "notes", "description", "comment", "additional_note")


def marker_plan(meta, model):
    """Where the marker goes: ("field", name) | ("chatter", None) | (None, None).

    Chatter comes before the generic fallback on purpose: on sale.order,
    account.move or purchase.order, `note` / `narration` hold the terms and
    conditions printed on the customer PDF.
    """
    if not HERMES_MARKER:
        return None, None
    name = MARKER_FIELDS.get(model)
    f = meta.get(name) if name else None
    if f and not f.get("readonly") and f.get("type") in ("char", "text", "html"):
        return "field", name
    if "message_ids" in meta:
        return "chatter", None
    for name in MARKER_FALLBACK_FIELDS:
        f = meta.get(name)
        if f and not f.get("readonly") and f.get("type") in ("text", "html"):
            return "field", name
    return None, None


def add_marker(vals, field, ftype):
    """Append the marker after any existing content (never replace it), once."""
    current = vals.get(field)
    current = current if isinstance(current, str) else ""
    if HERMES_MARKER in current:
        return
    if ftype == "html":
        vals[field] = current + "<p>%s</p>" % html.escape(HERMES_MARKER)
    elif ftype == "text":
        vals[field] = current + "\n" + HERMES_MARKER if current else HERMES_MARKER
    else:
        vals[field] = current + " " + HERMES_MARKER if current else HERMES_MARKER


def cmd_create(client, cfg, args):
    check_model_writable(cfg, args.model)
    if args.values and args.values.strip().startswith("["):
        raw_list = json.loads(args.values)
        if args.set:
            raise SafetyError("Use either a JSON list in --values or --set, not both.")
    else:
        raw_list = [gather_vals(args)]
    if not raw_list or not all(isinstance(v, dict) and v for v in raw_list):
        raise SafetyError("Nothing to create: give --values '{...}' or --set field=value.")
    if len(raw_list) > cfg.max_write:
        raise SafetyError("Refusing to create more than %d records at once." % cfg.max_write)
    meta = client.fields(args.model)
    prepared, notes = [], []
    for raw in raw_list:
        check_fields_writable(raw)
        vals, n = prepare_vals(client, args.model, raw)
        prepared.append(vals)
        notes += n
    mkind, mfield = marker_plan(meta, args.model)
    if HERMES_MARKER:
        if mkind == "field":
            for vals in prepared:
                add_marker(vals, mfield, meta[mfield]["type"])
            notes.append("marker '%s' added in field %s" % (HERMES_MARKER, mfield))
        elif mkind == "chatter":
            notes.append("marker '%s' will be posted as an internal chatter note" % HERMES_MARKER)
        else:
            notes.append("no place for marker '%s' on %s (no note field, no chatter)" % (HERMES_MARKER, args.model))
    required = [f for f, m in meta.items() if m.get("required") and not m.get("readonly")]
    defaults = {}
    try:
        defaults = client.call(args.model, "default_get", fields_list=required) or {}
    except OdooError:
        pass
    missing = sorted({f for vals in prepared for f in required if f not in vals and f not in defaults})
    preview = {"model": args.model, "values": prepared, "notes": notes, "missing_required": missing}

    def text():
        lines = ["Create %d %s record(s):" % (len(prepared), args.model)]
        for v in prepared:
            lines += ["  %-24s %s" % (k, fmt(val, 100)) for k, val in v.items()]
            lines.append("  --")
        lines += ["  note: " + n for n in notes]
        if missing:
            lines.append("  WARNING missing required fields without default: %s" % ", ".join(missing))
        return "\n".join(lines)
    if not args.apply:
        emit(args, dict(preview, dry_run=True), lambda: text() + DRY)
        return
    check_apply_allowed(cfg)
    res = client.call(args.model, "create", vals_list=prepared)
    ids = res if isinstance(res, list) else [res]
    if mkind == "chatter":
        for rid in ids:
            try:
                client.call(args.model, "message_post", ids=[rid], body=HERMES_MARKER,
                            message_type="comment", subtype_xmlid="mail.mt_note")
            except OdooError as e:  # the record exists: a marker failure must not fail the creation
                notes.append("could not post marker on #%s: %s" % (rid, e))
    entry = Journal(cfg).append({"op": "create", "model": args.model, "ids": ids, "values": prepared,
                                 "url": cfg.url, "db": client.db, "uid": client.uid, "note": args.reason})
    links = [client.link(args.model, i) for i in ids]
    emit(args, dict(preview, ids=ids, journal=entry, links=links),
         lambda: text() + "\nCreated ids %s  (journal %s)\n%s" % (ids, entry, "\n".join(links)))


def cmd_update(client, cfg, args):
    check_model_writable(cfg, args.model)
    raw = gather_vals(args)
    if not raw:
        raise SafetyError("Nothing to update: give --set field=value or --values '{...}'.")
    check_fields_writable(raw)
    vals, notes = prepare_vals(client, args.model, raw)
    ids = target_ids(client, cfg, args.model, args)
    meta = client.fields(args.model)
    fields = list(vals)
    before = {r["id"]: r for r in client.read(args.model, ids, fields + ["display_name"])}
    m2o_label = {}
    for f, new in vals.items():
        if meta[f]["type"] == "many2one" and isinstance(new, int) and not isinstance(new, bool):
            m2o_label[(f, new)] = [new, _names(client, meta[f]["relation"], [new]).get(new) or ""]
    changes, unchanged = [], []
    for rid in ids:
        rec = before.get(rid)
        if rec is None:
            raise SafetyError("Record %s #%s not found (or not accessible)." % (args.model, rid))
        diff = {}
        for f, new in vals.items():
            old = rec.get(f)
            is_cmd = meta[f]["type"] in ("one2many", "many2many")
            if is_cmd or norm(old) != norm(new):
                diff[f] = {"old": old, "new": m2o_label.get((f, new), new) if isinstance(new, int) else new}
        (changes if diff else unchanged).append({"id": rid, "name": rec.get("display_name"), "diff": diff})
    data = {"model": args.model, "values": vals, "changes": changes, "unchanged": [u["id"] for u in unchanged], "notes": notes}

    def text():
        lines = ["Update %s - %d record(s) would change, %d already up to date" % (args.model, len(changes), len(unchanged))]
        for c in changes:
            lines.append("  #%s %s" % (c["id"], c["name"] or ""))
            for f, d in c["diff"].items():
                lines.append("      %-22s %s  ->  %s" % (f, fmt(d["old"], 50) or "(empty)", fmt(d["new"], 50) or "(empty)"))
        lines += ["  note: " + n for n in notes]
        return "\n".join(lines)
    if not changes:
        emit(args, data, lambda: text() + "\nNothing to do.")
        return
    if not args.apply:
        emit(args, dict(data, dry_run=True), lambda: text() + DRY)
        return
    check_apply_allowed(cfg)
    target = [c["id"] for c in changes]
    client.call(args.model, "write", ids=target, vals=vals)
    after = {r["id"]: {f: r.get(f) for f in fields} for r in client.read(args.model, target, fields)}
    entry = Journal(cfg).append({
        "op": "write", "model": args.model, "ids": target, "values": vals,
        "types": {f: meta[f]["type"] for f in fields},
        "before": {str(i): {f: before[i].get(f) for f in fields} for i in target},
        "after": {str(i): v for i, v in after.items()},
        "url": cfg.url, "db": client.db, "uid": client.uid, "note": args.reason, "undo_of": getattr(args, "undo_of", None)})
    emit(args, dict(data, journal=entry), lambda: text() + "\nApplied. Journal entry %s (undo with: undo %s)" % (entry, entry))


def _set_active(client, cfg, args, active):
    check_model_writable(cfg, args.model)
    meta = client.fields(args.model)
    if "active" not in meta:
        raise SafetyError("%s has no 'active' field: it cannot be archived (and the skill never deletes)." % args.model)
    args.archived = not active  # when unarchiving, search among archived records
    ids = target_ids(client, cfg, args.model, args)
    recs = client.read(args.model, ids, ["display_name", "active"])
    todo = [r for r in recs if bool(r.get("active")) != active]
    verb = "Unarchive" if active else "Archive"
    data = {"model": args.model, "op": verb.lower(), "records": todo, "skipped": [r["id"] for r in recs if r not in todo]}

    def text():
        lines = ["%s %d %s record(s) (reversible):" % (verb, len(todo), args.model)]
        lines += ["  #%s %s" % (r["id"], r.get("display_name") or "") for r in todo]
        if data["skipped"]:
            lines.append("  already %s: %s" % ("active" if active else "archived", data["skipped"]))
        return "\n".join(lines)
    if not todo:
        emit(args, data, lambda: text() + "\nNothing to do.")
        return
    if not args.apply:
        emit(args, dict(data, dry_run=True), lambda: text() + DRY)
        return
    check_apply_allowed(cfg)
    ids = [r["id"] for r in todo]
    client.call(args.model, "write", ids=ids, vals={"active": active})
    entry = Journal(cfg).append({"op": "unarchive" if active else "archive", "model": args.model, "ids": ids,
                                 "url": cfg.url, "db": client.db, "uid": client.uid, "note": args.reason,
                                 "undo_of": getattr(args, "undo_of", None)})
    emit(args, dict(data, journal=entry), lambda: text() + "\nApplied. Journal entry %s (undo with: undo %s)" % (entry, entry))


def cmd_archive(client, cfg, args):
    _set_active(client, cfg, args, False)


def cmd_unarchive(client, cfg, args):
    _set_active(client, cfg, args, True)


def cmd_note(client, cfg, args):
    check_model_writable(cfg, args.model)
    if "message_ids" not in client.fields(args.model):
        raise SafetyError("%s has no chatter." % args.model)
    rid = int(args.id)
    name = _names(client, args.model, [rid]).get(rid)
    body = args.text if not args.signed else "%s\n\n— via Hermes" % args.text
    data = {"model": args.model, "id": rid, "name": name, "body": body, "kind": "internal note (not emailed to customers)"}
    text = lambda: "Post internal note on %s #%s %s:\n  %s" % (args.model, rid, name or "", body.replace("\n", "\n  "))
    if not args.apply:
        emit(args, dict(data, dry_run=True), lambda: text() + DRY)
        return
    check_apply_allowed(cfg)
    mid = client.call(args.model, "message_post", ids=[rid], body=body, message_type="comment",
                      subtype_xmlid="mail.mt_note")
    entry = Journal(cfg).append({"op": "note", "model": args.model, "ids": [rid], "message_id": mid, "body": body,
                                 "url": cfg.url, "db": client.db, "uid": client.uid})
    emit(args, dict(data, message_id=mid, journal=entry), lambda: text() + "\nPosted (message %s, journal %s)." % (mid, entry))


def cmd_activity(client, cfg, args):
    check_model_writable(cfg, args.model)
    if "activity_ids" not in client.fields(args.model):
        raise SafetyError("%s does not support activities." % args.model)
    rid = int(args.id)
    model_ids = client.call("ir.model", "search", domain=[["model", "=", args.model]], limit=1)
    if not model_ids:
        raise SafetyError("Unknown model %s." % args.model)
    type_id = None
    try:
        xmlid = client.search_read("ir.model.data", [["module", "=", "mail"], ["name", "=", "mail_activity_data_todo"]],
                                   ["res_id"], limit=1)
        type_id = xmlid[0]["res_id"] if xmlid else None
    except OdooError:
        pass
    if args.type:
        type_id = resolve_name(client, "mail.activity.type", args.type)
    if not type_id:
        types = client.call("mail.activity.type", "search", domain=[], limit=1)
        type_id = types[0] if types else False
    due = expand_macro(args.due, client) if args.due.startswith("@") else args.due
    user_id = resolve_name(client, "res.users", args.user) if args.user else client.uid
    vals = {"res_model_id": model_ids[0], "res_id": rid, "activity_type_id": type_id, "summary": args.summary,
            "date_deadline": due, "user_id": user_id}
    if args.note:
        vals["note"] = args.note
    if HERMES_MARKER:
        add_marker(vals, "note", "html")
    name = _names(client, args.model, [rid]).get(rid)
    data = {"target": "%s #%s %s" % (args.model, rid, name or ""), "values": vals}
    text = lambda: "Schedule activity on %s\n%s" % (data["target"], "\n".join("  %-16s %s" % (k, v) for k, v in vals.items()))
    if not args.apply:
        emit(args, dict(data, dry_run=True), lambda: text() + DRY)
        return
    check_apply_allowed(cfg)
    aid = client.call("mail.activity", "create", vals_list=[vals])
    aid = aid[0] if isinstance(aid, list) else aid
    entry = Journal(cfg).append({"op": "create", "model": "mail.activity", "ids": [aid], "values": [vals],
                                 "url": cfg.url, "db": client.db, "uid": client.uid, "note": "activity"})
    emit(args, dict(data, id=aid, journal=entry), lambda: text() + "\nScheduled (mail.activity #%s, journal %s)." % (aid, entry))


def cmd_call(client, cfg, args):
    method = args.method
    if method.startswith("_"):
        raise SafetyError("Private methods cannot be called.")
    kwargs = json.loads(args.kwargs) if args.kwargs else {}
    if not isinstance(kwargs, dict):
        raise SafetyError("--kwargs must be a JSON object.")
    ids = [int(x) for x in re.split(r"[,\s]+", args.ids.strip()) if x] if args.ids else None
    if method in READ_METHODS:
        res = client.call(args.model, method, ids=ids, **kwargs)
        emit(args, res, lambda: json.dumps(res, ensure_ascii=False, indent=2, default=str))
        return
    if HARD_BLOCKED_METHOD_RE.search(method):
        raise SafetyError("Method %r is blocked: this skill never runs destructive or irreversible methods. "
                          "Use update/archive, or ask a human to do it in Odoo." % method)
    check_model_writable(cfg, args.model)
    if method not in cfg.allowed_methods:
        raise SafetyError("Method %r is not allow-listed. The owner can add it to ODOO_HERMES_ALLOWED_METHODS "
                          "(comma-separated, e.g. action_confirm,action_post)." % method)
    if not ids:
        raise SafetyError("Business actions need --ids.")
    if len(ids) > cfg.max_write:
        raise SafetyError("Too many records (max %d)." % cfg.max_write)
    meta = client.fields(args.model)
    snap_fields = ["display_name"] + [f for f in ("state", "stage_id") if f in meta]
    before = client.read(args.model, ids, snap_fields)

    def text():
        lines = ["Call %s.%s(%s) on:" % (args.model, method, json.dumps(kwargs) if kwargs else "")]
        lines += ["  #%s %s  %s" % (r["id"], r.get("display_name") or "",
                                    " ".join("%s=%s" % (f, fmt(r.get(f))) for f in snap_fields[1:])) for r in before]
        lines.append("  WARNING: business actions may not be undoable automatically (they can send emails, post entries...).")
        return "\n".join(lines)
    if not args.apply:
        emit(args, {"dry_run": True, "records": before, "method": method}, lambda: text() + DRY)
        return
    check_apply_allowed(cfg)
    res = client.call(args.model, method, ids=ids, **kwargs)
    after = client.read(args.model, ids, snap_fields)
    entry = Journal(cfg).append({"op": "call", "model": args.model, "ids": ids, "method": method, "kwargs": kwargs,
                                 "before": before, "after": after, "url": cfg.url, "db": client.db, "uid": client.uid})
    emit(args, {"result": res, "after": after, "journal": entry},
         lambda: text() + "\nDone (journal %s).\n%s" % (entry, table(after, ["id"] + snap_fields)))


def cmd_journal(client, cfg, args):
    j = Journal(cfg)
    if args.show:
        e = j.get(args.show)
        emit(args, e, json.dumps(e, ensure_ascii=False, indent=2, default=str))
        return
    entries = j.entries()[-args.limit:]
    undone = {e.get("undo_of") for e in j.entries() if e.get("undo_of")}
    rows = [{"id": e["id"], "ts": e.get("ts"), "op": e.get("op"), "model": e.get("model"),
             "ids": e.get("ids"), "status": "undone" if e["id"] in undone else ("undo" if e.get("undo_of") else ""),
             "note": e.get("note") or e.get("method") or ""} for e in reversed(entries)]
    emit(args, rows, lambda: table(rows, ["id", "ts", "op", "model", "ids", "status", "note"]) + "\njournal file: %s" % j.path)


def cmd_undo(client, cfg, args):
    j = Journal(cfg)
    e = j.get(args.entry)
    if j.undone(e["id"]) and not args.force:
        raise SafetyError("Entry %s was already undone (use --force to undo again)." % e["id"])
    op, model = e.get("op"), e.get("model")
    ns = argparse.Namespace(model=model, apply=args.apply, json=args.json, reason="undo of %s" % e["id"],
                            undo_of=e["id"], where=None, archived=False, ids=",".join(str(i) for i in e["ids"]))
    if op == "write":
        types = e.get("types", {})
        current = {r["id"]: r for r in client.read(model, e["ids"], list(types))}
        conflicts = []
        per_record = {}
        for sid, old_vals in e["before"].items():
            rid = int(sid)
            after = e.get("after", {}).get(sid, {})
            cur = current.get(rid, {})
            for f in types:
                if types[f] != "one2many" and norm(cur.get(f)) != norm(after.get(f)):
                    conflicts.append("#%s %s changed since (now %s)" % (rid, f, fmt(cur.get(f))))
            per_record[rid] = {f: restore_value(v, types.get(f)) for f, v in old_vals.items() if types.get(f) != "one2many"}
        if conflicts and not args.force:
            raise SafetyError("Records changed after entry %s; undo could overwrite someone's work:\n  %s\n"
                              "Re-run with --force if you are sure." % (e["id"], "\n  ".join(conflicts)))
        if any(t == "one2many" for t in types.values()):
            print("note: lines added to one2many fields are kept (removing them could delete data).")
        groups = defaultdict(list)
        for rid, vals in per_record.items():
            groups[json.dumps(vals, sort_keys=True, default=str)].append(rid)
        for key, rids in groups.items():
            vals = json.loads(key)
            if not vals:
                continue
            ns.ids = ",".join(map(str, rids))
            ns.values, ns.set = json.dumps(vals), None
            _restore_write(client, cfg, ns, vals)
        return
    if op == "archive":
        return _set_active(client, cfg, ns, True)
    if op == "unarchive":
        return _set_active(client, cfg, ns, False)
    if op == "create":
        if "active" in client.fields(model):
            print("Undo of a creation = archive the created record(s) (nothing is deleted).")
            return _set_active(client, cfg, ns, False)
        raise SafetyError("%s has no 'active' field, so undoing this creation would require deleting it, which this "
                          "skill never does. Ask a human to delete %s %s in Odoo if needed." % (model, model, e["ids"]))
    raise SafetyError("Entry %s (%s) cannot be undone automatically: a human should review it in Odoo%s." % (
        e["id"], op, (" (%s)" % client.link(model, e["ids"][0])) if e.get("ids") else ""))


def _restore_write(client, cfg, ns, vals):
    """Write back previous values, with its own diff/dry-run + journal."""
    check_model_writable(cfg, ns.model)
    check_fields_writable([f for f in vals])
    ids = [int(i) for i in ns.ids.split(",")]
    meta = client.fields(ns.model)
    current = {r["id"]: r for r in client.read(ns.model, ids, list(vals) + ["display_name"])}
    lines = ["Restore %s on %s:" % (ns.model, ", ".join("#%s %s" % (i, current.get(i, {}).get("display_name", "")) for i in ids))]
    for f, v in vals.items():
        lines.append("    %-22s %s  ->  %s" % (f, fmt(current.get(ids[0], {}).get(f), 50) or "(empty)", fmt(v, 50) or "(empty)"))
    if not ns.apply:
        emit(ns, {"dry_run": True, "restore": vals, "ids": ids}, "\n".join(lines) + DRY)
        return
    check_apply_allowed(cfg)
    client.call(ns.model, "write", ids=ids, vals=vals)
    fields = list(vals)
    after = {r["id"]: {f: r.get(f) for f in fields} for r in client.read(ns.model, ids, fields)}
    entry = Journal(cfg).append({
        "op": "write", "model": ns.model, "ids": ids, "values": vals, "types": {f: meta[f]["type"] for f in fields},
        "before": {str(i): {f: current[i].get(f) for f in fields} for i in ids},
        "after": {str(i): v for i, v in after.items()}, "url": cfg.url, "db": client.db, "uid": client.uid,
        "note": ns.reason, "undo_of": ns.undo_of})
    emit(ns, {"restored": vals, "ids": ids, "journal": entry}, "\n".join(lines) + "\nRestored. Journal entry %s." % entry)


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def build_parser():
    common = argparse.ArgumentParser(add_help=False)
    # SUPPRESS so a subcommand does not reset a global `--json` given before it.
    common.add_argument("--json", action="store_true", default=argparse.SUPPRESS, help="machine-readable JSON output")
    writes = argparse.ArgumentParser(add_help=False)
    writes.add_argument("--apply", action="store_true", help="really execute (default is a dry-run preview)")
    writes.add_argument("--reason", help="short note stored in the journal")
    target = argparse.ArgumentParser(add_help=False)
    target.add_argument("--ids", help="comma-separated record ids")
    target.add_argument("--where", help="query (mini-DSL or JSON domain) selecting the records")

    p = argparse.ArgumentParser(prog="odoo_cli.py", description="Safe, non-destructive Odoo bridge for Hermes Agent.",
                                )
    p.add_argument("--json", action="store_true", help="machine-readable JSON output")
    p.add_argument("--version", action="version", version=__version__)
    sub = p.add_subparsers(dest="cmd", metavar="command")

    def add(name, fn, help_, parents=()):
        sp = sub.add_parser(name, help=help_, parents=[common, *parents])
        sp.set_defaults(fn=fn)
        return sp

    add("doctor", cmd_doctor, "check connection, version, user, apps and safety settings")
    add("whoami", cmd_whoami, "current Odoo user")
    sp = add("models", cmd_models, "search models by technical name or label")
    sp.add_argument("query", nargs="?")
    sp.add_argument("--limit", type=int, default=50)
    sp = add("fields", cmd_fields, "list fields of a model")
    sp.add_argument("model")
    sp.add_argument("--filter")
    sp.add_argument("--required", action="store_true")
    sp = add("find", cmd_find, "search records with the mini query language")
    sp.add_argument("model")
    sp.add_argument("where", nargs="?", default="")
    sp.add_argument("--fields")
    sp.add_argument("--limit", type=int, default=20)
    sp.add_argument("--offset", type=int, default=0)
    sp.add_argument("--order")
    sp.add_argument("--archived", action="store_true", help="include archived records")
    sp = add("get", cmd_get, "read full records")
    sp.add_argument("model")
    sp.add_argument("ids", nargs="+")
    sp.add_argument("--fields", help="comma list, or 'all'")
    sp.add_argument("--show-empty", action="store_true")
    sp = add("count", cmd_count, "count matching records")
    sp.add_argument("model")
    sp.add_argument("where", nargs="?", default="")
    sp = add("group", cmd_group, "group & aggregate (e.g. --by stage_id --sum expected_revenue)")
    sp.add_argument("model")
    sp.add_argument("where", nargs="?", default="")
    sp.add_argument("--by", required=True, help="field[,field] (date fields accept :month, :week, :year)")
    sp.add_argument("--sum")
    sp.add_argument("--order")
    sp = add("lookup", cmd_lookup, "omni-search a text across the main business models")
    sp.add_argument("text")
    sp.add_argument("--models")
    sp.add_argument("--limit", type=int, default=5)
    add("pulse", cmd_pulse, "business heartbeat: sales, overdue invoices, pipeline, late transfers...")
    sp = add("todo", cmd_todo, "my activities (late, today, upcoming)")
    sp.add_argument("--days", type=int, default=7)
    sp.add_argument("--limit", type=int, default=50)
    sp.add_argument("--everyone", action="store_true")
    sp = add("history", cmd_history, "chatter & tracked changes of a record")
    sp.add_argument("model")
    sp.add_argument("id")
    sp.add_argument("--limit", type=int, default=15)
    sp = add("360", cmd_360, "customer 360: contact, sales, unpaid invoices, opportunities, activities")
    sp.add_argument("partner", help="name or id")
    sp = add("hygiene", cmd_hygiene, "read-only contact data-quality audit")
    sp.add_argument("--scan", type=int, default=20000)
    sp.add_argument("--top", type=int, default=10)
    sp = add("export", cmd_export, "export records to a local CSV (never overwrites)")
    sp.add_argument("model")
    sp.add_argument("where", nargs="?", default="")
    sp.add_argument("--fields")
    sp.add_argument("--limit", type=int, default=5000)
    sp.add_argument("--order")
    sp.add_argument("--out")
    sp.add_argument("--overwrite", action="store_true")

    sp = add("create", cmd_create, "create record(s) [dry-run unless --apply]", [writes])
    sp.add_argument("model")
    sp.add_argument("--values", help="JSON object (or list of objects)")
    sp.add_argument("--set", action="append", help="field=value (repeatable). '@Name' resolves a many2one by name")
    sp = add("update", cmd_update, "update records with a diff preview [dry-run unless --apply]", [writes, target])
    sp.add_argument("model")
    sp.add_argument("--values")
    sp.add_argument("--set", action="append")
    sp.add_argument("--archived", action="store_true")
    sp = add("archive", cmd_archive, "archive records (the non-destructive 'delete')", [writes, target])
    sp.add_argument("model")
    sp = add("unarchive", cmd_unarchive, "restore archived records", [writes, target])
    sp.add_argument("model")
    sp = add("note", cmd_note, "post an internal note in a record's chatter", [writes])
    sp.add_argument("model")
    sp.add_argument("id")
    sp.add_argument("text")
    sp.add_argument("--signed", action="store_true", help="append '— via Hermes'")
    sp = add("activity", cmd_activity, "schedule a follow-up activity on a record", [writes])
    sp.add_argument("model")
    sp.add_argument("id")
    sp.add_argument("--summary", required=True)
    sp.add_argument("--due", default="@today+1", help="YYYY-MM-DD or macro like @today+3")
    sp.add_argument("--note")
    sp.add_argument("--type", help="activity type name (default: To-Do)")
    sp.add_argument("--user", help="assignee (name, login or id; default: me)")
    sp = add("call", cmd_call, "call a model method (read methods free; actions allow-listed)", [writes])
    sp.add_argument("model")
    sp.add_argument("method")
    sp.add_argument("--ids")
    sp.add_argument("--kwargs", help="JSON object of keyword arguments")
    sp = add("journal", cmd_journal, "list applied changes (local, append-only)")
    sp.add_argument("--limit", type=int, default=20)
    sp.add_argument("--show")
    sp = add("undo", cmd_undo, "revert a journal entry [dry-run unless --apply]", [writes])
    sp.add_argument("entry")
    sp.add_argument("--force", action="store_true", help="ignore conflict detection")
    return p


OFFLINE = {cmd_journal}


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "fn", None):
        parser.print_help()
        return 2
    cfg = Config()
    # Config() loads ~/.hermes/.env: read the marker only now, or a value set there would be ignored.
    global HERMES_MARKER
    HERMES_MARKER = os.environ.get("ODOO_HERMES_MARKER", "MadeByHermes")
    client = Client(cfg)
    try:
        if args.fn is not cmd_doctor and args.fn not in OFFLINE:
            client.connect()
        args.fn(client, cfg, args)
        return 0
    except SafetyError as e:
        print("REFUSED (safety): %s" % e, file=sys.stderr)
        return 3
    except OdooError as e:
        print("ODOO ERROR: %s" % e, file=sys.stderr)
        return 4
    except KeyboardInterrupt:
        return 130
    except (ValueError, SyntaxError) as e:
        print("INPUT ERROR: %s" % e, file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
