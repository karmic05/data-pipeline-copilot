{{
    config(
        materialized='incremental'
    )
}}

-- Incremental model with NO incremental_strategy and no is_incremental() guard,
-- so every run rebuilds the whole table instead of merging the new slice.
SELECT
    o.order_id,
    o.order_ts,
    c.customer_id,
    c.customer_email,
    o.order_total,
    o.status
FROM {{ source('raw', 'orders') }} o
JOIN {{ ref('stg_customers') }} c
    ON o.customer_id = c.customer_id
WHERE o.status = 'complete'

--- schema.yml
version: 2

sources:
  - name: raw
    tables:
      - name: orders

models:
  - name: dbt_orders
    columns:
      - name: order_id
      - name: customer_email
      - name: order_total
