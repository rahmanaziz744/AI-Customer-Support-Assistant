---
slug: order-status-policy
title: Order Status and Tracking Policy
category: ORDER_STATUS
version: "1.0"
rules:
  tracking_available_after_hours: 24
  status_values: ["PLACED", "SHIPPED", "DELIVERED", "CANCELLED", "RETURNED"]
  no_action_required: true
---

# Order Status and Tracking Policy

## Answering a status question

Order status questions are informational and require **no refund, replacement,
or other action**. Look up the order, state its current status plainly, and give
the tracking number and carrier if the order has shipped. Do not offer
compensation for a question that only asked where an order is.

## Status meanings

- **PLACED** — payment authorised, not yet handed to the carrier.
- **SHIPPED** — with the carrier; tracking is live.
- **DELIVERED** — carrier has confirmed delivery.
- **CANCELLED** — cancelled before shipment; refund issued automatically.
- **RETURNED** — returned to us; refund follows under the Refund Policy.

## Tracking availability

Tracking numbers become scannable up to **24 hours** after an order ships. A
tracking number that shows "not found" immediately after shipping is normal and
not evidence of a problem. Tell the customer to check again the next day.

## When a status question is really something else

Customers often frame a complaint as a status question. If the order is past its
delivery estimate, treat it under the Shipping and Delivery Policy rather than
answering the literal question. If tracking shows delivered but the customer
says it did not arrive, that is a lost-package case, not a status case.

## Unknown order references

If the order reference in the ticket does not exist, do not guess or search by
partial match. Ask the customer to confirm the reference from their confirmation
email, and escalate if they insist the reference is correct.
