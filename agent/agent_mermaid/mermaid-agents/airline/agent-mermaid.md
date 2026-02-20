```mermaid
flowchart TD
    intake --> classify
    classify --> general_info
    classify --> new_booking
    classify --> modify_booking
    classify --> cancel_booking
    classify --> handle_complaint
    classify --> escalate
    new_booking --> confirm
    modify_booking --> confirm
    cancel_booking --> confirm
    handle_complaint --> confirm
    handle_complaint --> escalate
    general_info --> confirm
```
