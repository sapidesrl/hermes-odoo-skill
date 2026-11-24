# Safety model (for the Odoo owner)

The skill assumes that an AI agent can misunderstand a request, be tricked by text it reads in
Odoo (prompt injection), or simply make a mistake. So every layer below is enforced **in code**
(`scripts/odoo_cli.py`), not just in the instructions given to the agent.

## 1. Start with a dedicated Odoo user
The strongest guard is Odoo's own access rights. Create a user such as "Hermes (AI assistant)"
with only the apps and rights it needs (e.g. *Sales: User - own documents only*), and generate an
API key for that user. Everything the skill does is logged in Odoo under that user.

## 2. Settings (in `~/.hermes/.env`)

| Variable | Default | Effect |
|---|---|---|
| `ODOO_HERMES_ALLOW_WRITES` | off | `1` allows writes. Without it the skill is strictly read-only. |
| `ODOO_HERMES_MAX_WRITE` | `25` | Max records per write operation (hard ceiling 200). |
| `ODOO_HERMES_ALLOWED_METHODS` | empty | Comma list of business methods the agent may call, e.g. `action_confirm,action_post`. |
| `ODOO_HERMES_PROTECTED_MODELS` | empty | Extra models to protect from any write, e.g. `hr.employee,product.pricelist`. |
| `ODOO_HERMES_HOME` | `$HERMES_HOME/odoo` (`~/.hermes/odoo`) | Where the journal lives. |
| `ODOO_HERMES_MARKER` | `MadeByHermes` | Text added to every record created by the skill; empty disables it. |
| `ODOO_PROTOCOL` | `auto` | `xmlrpc`, `json2` (Odoo 19+, API key) or `auto`. |
| `ODOO_VERIFY_SSL` | `1` | Set `0` only for a self-signed test server. |

The settings can only make the skill *more* permissive within fixed limits; the hard rules
below cannot be disabled by configuration.

## 3. Hard rules (not configurable)
- No `unlink`, ever. Methods whose name contains `unlink`, `delete`, `uninstall`, `purge`,
  `drop`, `reset`, `cancel`, `refund`, `reverse`, `void`, `revoke`, `remove`, `clear`, `merge`,
  `sudo`, `toggle_active`... are refused even if allow-listed. Raw `write`/`create` through `call`
  are refused too (they must go through the previewed commands).
- Protected models: `ir.*`, `base.*`, `res.users*`, `res.groups`, `res.company`, `res.config*`,
  `bus.*`, `iap.*`, mail servers, payment providers, accounting journals, chart of accounts, taxes.
- Protected fields: passwords, API keys, secrets, tokens, TOTP, signup, groups, `share`, `state`,
  `active`, `company_id(s)`, `move_type`, audit fields.
- x2many: commands `2` (delete record) and `5` (clear all) are refused; one2many only accepts
  create/update/link (removing a line may delete it).
- An empty `--where` (= every record) is refused for writes.
- `export` never overwrites a file.

## 4. Preview -> confirm -> apply
Every write is a dry run unless `--apply` is passed, and the agent is instructed to show the
preview and wait for an explicit yes.

## 5. Journal and undo
`~/.hermes/odoo/journal.jsonl` (mode 600) is append-only. Each entry stores the operation, the
records, the values written, and the values *before* and *after*. `undo <id>`:
- restores previous values of an `update` (refusing if someone modified the record since);
- unarchives what was archived, re-archives what was unarchived;
- archives what was created (if the model supports archiving);
- for notes and business actions: explains what a human should review (they cannot be reverted
  without deleting or cancelling, which the skill never does).

## 6. Traceability marker
Every record created by `create` or `activity` is tagged with `ODOO_HERMES_MARKER`, never in a
field printed for the customer, in this order:
1. the model's internal-notes field, if known (`res.partner.comment`, `crm.lead.description`,
   `project.task.description`, `hr.employee.additional_note`, `mail.activity.note`...): appended
   after existing content, never replacing it, never duplicated;
2. otherwise an internal note in the chatter (e.g. `sale.order`, `account.move`,
   `purchase.order`, whose `note` / `narration` are the terms and conditions printed on the PDF);
   if posting it fails the record is still created and the failure is reported;
3. otherwise the first writable `note`, `notes`, `description`, `comment` or `additional_note`
   text/html field;
4. otherwise no marker, and the preview says so.

## 7. What the skill will not protect you from
- Odoo automations (server actions, automated rules) triggered by a legitimate write.
- Business actions you allow-list: `action_quotation_send` sends an email, `action_post` posts an
  invoice. Only allow-list what you are comfortable delegating.
