```mermaid
flowchart TD
    intake --> classify
    classify --> general_info
    classify --> cancel_order
    classify --> modify_order
    classify --> return_order
    classify --> exchange_order
    classify --> modify_address
    classify --> escalate
    general_info --> confirm
    cancel_order --> confirm
    modify_order --> confirm
    return_order --> confirm
    exchange_order --> confirm
    modify_address --> confirm
    escalate --> confirm
```
