"""A tiny in-memory Odoo look-alike speaking XML-RPC, for offline tests."""
import threading
import xmlrpc.client
from xmlrpc.server import SimpleXMLRPCRequestHandler, SimpleXMLRPCServer

FIELDS = {
    "res.partner": {
        "name": {"type": "char", "string": "Name", "required": True},
        "display_name": {"type": "char", "string": "Display Name", "readonly": True},
        "email": {"type": "char", "string": "Email"},
        "phone": {"type": "char", "string": "Phone"},
        "zip": {"type": "char", "string": "Zip"},
        "comment": {"type": "html", "string": "Notes"},
        "active": {"type": "boolean", "string": "Active"},
        "is_company": {"type": "boolean", "string": "Is company"},
        "type": {"type": "selection", "string": "Type", "selection": [["contact", "Contact"], ["invoice", "Invoice"]]},
        "parent_id": {"type": "many2one", "string": "Parent", "relation": "res.partner"},
        "category_id": {"type": "many2many", "string": "Tags", "relation": "res.partner.category"},
        "child_ids": {"type": "one2many", "string": "Contacts", "relation": "res.partner"},
        "message_ids": {"type": "one2many", "string": "Messages", "relation": "mail.message"},
        "activity_ids": {"type": "one2many", "string": "Activities", "relation": "mail.activity"},
    },
    "res.partner.category": {"name": {"type": "char", "string": "Name"}, "display_name": {"type": "char"}},
    "res.users": {"name": {"type": "char"}, "login": {"type": "char"}, "display_name": {"type": "char"},
                  "company_id": {"type": "many2one", "relation": "res.company"}, "email": {"type": "char"},
                  "tz": {"type": "char"}, "lang": {"type": "char"}},
    "mail.message": {"model": {"type": "char"}, "res_id": {"type": "integer"}, "body": {"type": "html"},
                     "date": {"type": "datetime"}, "author_id": {"type": "many2one", "relation": "res.partner"},
                     "message_type": {"type": "char"}, "subtype_id": {"type": "many2one", "relation": "mail.message.subtype"},
                     "tracking_value_ids": {"type": "one2many", "relation": "mail.tracking.value"}},
    "ir.model": {"model": {"type": "char"}, "name": {"type": "char"}},
    # `note` = terms and conditions printed on the customer PDF: must never get the marker.
    "sale.order": {"name": {"type": "char", "string": "Order"}, "display_name": {"type": "char"},
                   "partner_id": {"type": "many2one", "relation": "res.partner"},
                   "note": {"type": "html", "string": "Terms and conditions"},
                   "message_ids": {"type": "one2many", "relation": "mail.message"}},
    "mail.activity.type": {"name": {"type": "char"}, "display_name": {"type": "char"}},
    "ir.model.data": {"module": {"type": "char"}, "name": {"type": "char"}, "res_id": {"type": "integer"}},
    "mail.activity": {"res_model_id": {"type": "many2one", "relation": "ir.model"}, "res_id": {"type": "integer"},
                      "summary": {"type": "char"}, "date_deadline": {"type": "date"},
                      "user_id": {"type": "many2one", "relation": "res.users"},
                      "activity_type_id": {"type": "many2one", "relation": "mail.activity.type"},
                      "res_model": {"type": "char"}, "res_name": {"type": "char"}, "note": {"type": "html"}},
}


class FakeOdoo:
    def __init__(self):
        self.calls = []
        self.data = {
            "res.partner": {
                1: {"name": "Acme Corp", "email": "info@acme.test", "phone": "111", "active": True, "is_company": True,
                    "type": "contact", "parent_id": False, "category_id": [1], "zip": False},
                2: {"name": "John Acme", "email": "john@acme.test", "phone": False, "active": True, "is_company": False,
                    "type": "contact", "parent_id": 1, "category_id": [], "zip": False},
                3: {"name": "Globex", "email": "info@acme.test", "phone": "333", "active": True, "is_company": True,
                    "type": "contact", "parent_id": False, "category_id": [], "zip": False},
                4: {"name": "Old Ltd", "email": "bad-email", "phone": False, "active": False, "is_company": True,
                    "type": "contact", "parent_id": False, "category_id": [], "zip": False},
            },
            "res.partner.category": {1: {"name": "VIP"}, 2: {"name": "Prospect"}},
            "res.users": {2: {"name": "Admin", "login": "admin", "company_id": False, "email": "a@x", "tz": "UTC", "lang": "en_US"}},
            "mail.message": {},
            "ir.model": {1: {"model": "res.partner", "name": "Contact"}},
            "mail.activity": {},
            "sale.order": {},
            "mail.activity.type": {1: {"name": "To-Do"}},
            "ir.model.data": {1: {"module": "mail", "name": "mail_activity_data_todo", "res_id": 1}},
        }

    # ---------------- helpers
    def _match(self, model, rec_id, rec, leaf):
        f, op, v = leaf
        if f == "id":
            val = rec_id
        else:
            val = rec.get(f, False)
        if isinstance(val, list) and op in ("=", "!=") and v is False:
            val = val or False
        if op == "=":
            return val == v
        if op == "!=":
            return val != v
        if op == "ilike":
            return isinstance(val, str) and str(v).lower() in val.lower()
        if op == "not ilike":
            return not (isinstance(val, str) and str(v).lower() in val.lower())
        if op == "in":
            return val in v
        if op == "not in":
            return val not in v
        if op == "child_of":
            return rec_id == v or rec.get("parent_id") == v
        if op in (">", "<", ">=", "<="):
            return val is not False and eval("val %s v" % op)
        raise xmlrpc.client.Fault(1, "unsupported operator %s" % op)

    def _search(self, model, domain, context):
        context = context or {}
        domain = [list(d) if isinstance(d, (list, tuple)) else d for d in domain]
        out = []
        has_active = "active" in FIELDS.get(model, {})
        for rid, rec in sorted(self.data[model].items()):
            if has_active and context.get("active_test", True) and not any(
                    isinstance(d, list) and d[0] == "active" for d in domain) and not rec.get("active", True):
                continue
            stack = list(domain)
            ok = self._eval(model, rid, rec, stack)
            if ok:
                out.append(rid)
        return out

    def _eval(self, model, rid, rec, domain):
        # supports implicit AND and prefix '|'
        def consume(i):
            item = domain[i]
            if item == "|":
                a, i2 = consume(i + 1)
                b, i3 = consume(i2)
                return a or b, i3
            if item == "&":
                a, i2 = consume(i + 1)
                b, i3 = consume(i2)
                return a and b, i3
            return self._match(model, rid, rec, item), i + 1
        i, result = 0, True
        while i < len(domain):
            r, i = consume(i)
            result = result and r
        return result

    def _read(self, model, ids, fields):
        out = []
        for rid in ids:
            rec = self.data[model].get(rid)
            if rec is None:
                continue
            row = {"id": rid}
            for f in fields or list(FIELDS[model]):
                meta = FIELDS[model].get(f, {})
                if f == "display_name":
                    row[f] = rec.get("name") or "%s,%s" % (model, rid)
                elif meta.get("type") == "many2one":
                    v = rec.get(f)
                    rel = meta["relation"]
                    row[f] = [v, (self.data.get(rel, {}).get(v) or {}).get("name", "x")] if v else False
                elif meta.get("type") in ("one2many",) and f not in rec:
                    row[f] = []
                else:
                    row[f] = rec.get(f, False)
            out.append(row)
        return out

    def _apply_x2m(self, model, field, cmds, current):
        cur = list(current or [])
        for c in cmds:
            code = c[0]
            if code == 4 and c[1] not in cur:
                cur.append(c[1])
            elif code == 6:
                cur = list(c[2])
            elif code == 3:
                cur = [x for x in cur if x != c[1]]
            else:
                raise xmlrpc.client.Fault(1, "fake: unsupported command %s" % code)
        return cur

    def _write(self, model, ids, vals):
        for rid in ids:
            rec = self.data[model][rid]
            for k, v in vals.items():
                if FIELDS[model].get(k, {}).get("type") == "many2many":
                    rec[k] = self._apply_x2m(model, k, v, rec.get(k))
                else:
                    rec[k] = v
        return True

    # ---------------- RPC entry points
    def version(self):
        return {"server_version": "17.0", "server_version_info": [17, 0, 0, "final", 0, ""]}

    def authenticate(self, db, login, password, ctx):
        return 2 if (login == "admin" and password == "secret") else False

    def execute_kw(self, db, uid, pwd, model, method, args, kw):
        self.calls.append((model, method, args, kw))
        ctx = kw.pop("context", {}) or {}
        if method == "unlink":
            raise xmlrpc.client.Fault(1, "TEST: unlink must never be called")
        if model not in self.data:
            raise xmlrpc.client.Fault(1, "Object %s doesn't exist" % model)
        if method == "fields_get":
            return FIELDS[model]
        if method == "search":
            ids = self._search(model, args[0] if args else kw.get("domain", []), ctx)
            return ids[: kw["limit"]] if kw.get("limit") else ids
        if method == "search_count":
            return len(self._search(model, args[0], ctx))
        if method == "search_read":
            ids = self._search(model, args[0] if args else kw.get("domain", []), ctx)
            ids = ids[kw.get("offset", 0):]
            if kw.get("limit"):
                ids = ids[: kw["limit"]]
            return self._read(model, ids, kw.get("fields"))
        if method == "read":
            return self._read(model, args[0], kw.get("fields"))
        if method == "name_search":
            name, op = kw.get("name", ""), kw.get("operator", "ilike")
            res = []
            for rid in self._search(model, [], ctx):
                n = self.data[model][rid].get("name") or ""
                if (op == "=" and n == name) or (op == "ilike" and name.lower() in n.lower()):
                    res.append([rid, n])
            return res[: kw.get("limit", 100)]
        if method == "default_get":
            return {}
        if method == "create":
            ids = []
            for vals in args[0]:
                rid = max(self.data[model] or {0: 0}) + 1
                rec = {"active": True} if "active" in FIELDS[model] else {}
                rec.update(vals)
                self.data[model][rid] = rec
                ids.append(rid)
            return ids
        if method == "write":
            return self._write(model, args[0], args[1])
        if method == "message_post":
            mid = max(self.data["mail.message"] or {0: 0}) + 1
            self.data["mail.message"][mid] = {"model": model, "res_id": args[0][0], "body": kw["body"],
                                              "message_type": "comment", "date": "2026-01-01 00:00:00"}
            return mid
        if method == "action_bump":
            return None and True  # returns None -> server refuses to marshal
        raise xmlrpc.client.Fault(1, "fake: method %s not implemented" % method)


class _Handler(SimpleXMLRPCRequestHandler):
    rpc_paths = ("/xmlrpc/2/common", "/xmlrpc/2/object", "/xmlrpc/2/db")

    def log_message(self, *a):
        pass


def start():
    fake = FakeOdoo()
    server = SimpleXMLRPCServer(("127.0.0.1", 0), requestHandler=_Handler, allow_none=False, logRequests=False)
    server.register_instance(fake)
    server.register_function(lambda: ["testdb"], "list")
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    return fake, server, "http://127.0.0.1:%d" % server.server_address[1]


# ---------------------------------------------------------------- JSON-2 (Odoo 19+)
import json as _json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def start_json2(api_key="k3y"):
    fake = FakeOdoo()

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, code, obj):
            raw = _json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_POST(self):
            body = _json.loads(self.rfile.read(int(self.headers["Content-Length"])) or b"{}")
            if self.path == "/web/webclient/version_info":
                return self._send(200, {"jsonrpc": "2.0", "result": {"server_version": "19.0"}})
            if self.headers.get("Authorization") != "bearer %s" % api_key:
                return self._send(401, {"name": "AccessDenied", "message": "bad key"})
            _, _, _, model, method = self.path.split("/", 4)
            ids = body.pop("ids", None)
            if method == "create":
                args = [body.pop("vals_list")]
            elif method == "write":
                args = [ids, body.pop("vals")]
            elif method == "search_count":
                args = [body.pop("domain")]
            else:
                args = [ids] if ids is not None else []
            try:
                return self._send(200, fake.execute_kw("db", 2, api_key, model, method, args, body))
            except xmlrpc.client.Fault as e:
                return self._send(422, {"name": "UserError", "message": e.faultString})

    server = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return fake, server, "http://127.0.0.1:%d" % server.server_address[1]
