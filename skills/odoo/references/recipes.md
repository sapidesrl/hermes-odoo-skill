# Ready-made queries

```bash
# Sales
$ODOO find sale.order "state in ['draft','sent'], validity_date < @today" --fields name,partner_id,amount_total,validity_date   # expired quotes
$ODOO group sale.order "state = sale, date_order >= @year_start" --by user_id --sum amount_total                              # sales per salesperson
$ODOO group sale.order "state = sale" --by partner_id --sum amount_total --order "amount_total desc"                           # best customers

# Invoicing
$ODOO find account.move "move_type = out_invoice, state = posted, payment_state in ['not_paid','partial'], invoice_date_due < @today-30" \
      --fields name,partner_id,invoice_date_due,amount_residual --order "invoice_date_due asc"                                # 30+ days overdue
$ODOO group account.move "move_type = out_invoice, state = posted, payment_state in ['not_paid','partial']" --by partner_id --sum amount_residual
$ODOO find account.move "move_type = in_invoice, state = draft" --fields name,partner_id,amount_total                        # bills to validate

# CRM
$ODOO find crm.lead "type = opportunity, date_deadline < @today" --fields name,partner_id,stage_id,expected_revenue,user_id     # slipped deals
$ODOO find crm.lead "type = opportunity, !activity_ids" --fields name,partner_id,stage_id,user_id                             # deals with no next step
$ODOO group crm.lead "stage_id.is_won = true, date_closed >= @year_start" --by date_closed:month --sum expected_revenue          # won per month
$ODOO find crm.lead "type = lead, create_date >= @today-7" --fields name,contact_name,email_from,source_id                     # new leads this week

# Inventory
$ODOO find stock.picking "state in ['confirmed','assigned','waiting'], scheduled_date < @now" --fields name,partner_id,scheduled_date,origin
$ODOO find product.product "qty_available <= 0, sale_ok = true" --fields default_code,name,qty_available,virtual_available

# Projects
$ODOO find project.task "date_deadline < @today, stage_id.fold = false" --fields name,project_id,user_ids,date_deadline
$ODOO group project.task "stage_id.fold = false" --by project_id

# Contacts
$ODOO find res.partner "is_company = true, !vat" --fields name,country_id
$ODOO find res.partner "email =ilike %@gmail.com" --fields name,email
```

## Safe write patterns

```bash
# 1. Inspect  2. Preview  3. Confirm with the user  4. Apply  5. Report the journal id
$ODOO find crm.lead "name ~ 'ACME renewal'"
$ODOO update crm.lead --ids 314 --set expected_revenue=18000 --set priority=2
#   ... user says yes ...
$ODOO update crm.lead --ids 314 --set expected_revenue=18000 --set priority=2 --apply --reason "renewal re-estimated"
$ODOO journal --limit 1
```
