# Common Odoo models and fields

Field names move between versions. When in doubt: `fields <model> --filter <word>`.

## Contacts - `res.partner`
`name`, `email`, `phone`, `mobile` (≤17), `is_company`, `parent_id` (company of a contact),
`child_ids` (contacts of a company), `street`, `city`, `zip`, `country_id`, `vat`,
`category_id` (tags), `user_id` (salesperson), `customer_rank`, `supplier_rank`, `active`.
Tip: `partner_id child_of <company id>` matches a company and all its contacts.

## CRM - `crm.lead`
`type` (`lead` / `opportunity`), `name`, `partner_id`, `contact_name`, `email_from`, `phone`,
`stage_id` (`stage_id.is_won`), `expected_revenue`, `probability`, `priority` (`'0'`..`'3'`),
`user_id`, `team_id`, `date_deadline` (expected closing), `date_closed`, `tag_ids`, `lost_reason_id`.

## Sales - `sale.order`, `sale.order.line`
Order: `name`, `partner_id`, `date_order`, `validity_date`, `state` (`draft`, `sent`, `sale`,
`done` ≤16, `cancel`), `amount_untaxed`, `amount_total`, `currency_id`, `user_id`,
`invoice_status`, `order_line`.
Line: `order_id`, `product_id`, `name`, `product_uom_qty`, `price_unit`, `discount`, `price_subtotal`.
Actions often allow-listed: `action_confirm`, `action_quotation_send` (sends email!).

## Invoicing - `account.move`, `account.move.line`
`move_type` (`out_invoice`, `out_refund`, `in_invoice`, `in_refund`, `entry`), `name`, `partner_id`,
`invoice_date`, `invoice_date_due`, `state` (`draft`, `posted`, `cancel`),
`payment_state` (`not_paid`, `partial`, `in_payment`, `paid`, `reversed`), `amount_total`,
`amount_residual`, `currency_id`, `invoice_line_ids`, `ref`.
Posted entries are locked by Odoo itself; drafts can be edited.

## Purchase - `purchase.order`
`name`, `partner_id`, `date_order`, `date_planned`, `state` (`draft`, `sent`, `to approve`,
`purchase`, `done`, `cancel`), `amount_total`, `order_line`.

## Inventory - `stock.picking`, `stock.quant`, `product.product`
Picking: `name`, `partner_id`, `picking_type_id`, `scheduled_date`, `date_done`, `origin`,
`state` (`draft`, `waiting`, `confirmed`, `assigned`, `done`, `cancel`).
Quant: `product_id`, `location_id`, `quantity`, `reserved_quantity`.
Product: `name`, `default_code`, `barcode`, `list_price`, `standard_price`, `qty_available`,
`virtual_available`, `type` / `detailed_type` / `is_storable` (18+), `categ_id`.

## Projects - `project.project`, `project.task`
Task: `name`, `project_id`, `stage_id` (`stage_id.fold` = closed column), `user_ids` (16+) or
`user_id` (≤15), `date_deadline`, `priority`, `tag_ids`, `parent_id`, `state` (17+).

## Others
- Helpdesk (Enterprise): `helpdesk.ticket` - `name`, `partner_id`, `team_id`, `stage_id`, `priority`, `user_id`.
- HR: `hr.employee` - `name`, `job_title`, `department_id`, `parent_id`, `work_email`.
- Activities: `mail.activity` - `res_model`, `res_id`, `res_name`, `summary`, `activity_type_id`,
  `date_deadline`, `user_id`.
- Chatter: `mail.message` - `model`, `res_id`, `body`, `author_id`, `message_type`, `subtype_id`, `date`.
