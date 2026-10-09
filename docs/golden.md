# Independent golden set

    PASS G01_inventory_netting: Inventory reduces purchase shortfall
    PASS G02_existing_po_netting: Existing PO reduces shortfall
    PASS G03_asl_hard_filter: Unapproved supplier is never used
    PASS G04_iso_for_electronics: Electronic component requires ISO-9001
    PASS G05_iec_for_capacitor_pack: DC link capacitor pack requires ISO-9001 and IEC-62368
    PASS G06_mexico_is_domestic: Mexico counts as domestic even if the DB flag says international
    PASS G07_international_when_domestic_premium_gt30: Non-critical international supplier allowed when the domestic premium exceeds 30%
    PASS G08_critical_uses_45pct_threshold: Critical part keeps the domestic supplier below a 45% premium
    PASS G09_moq_floor: MOQ rounds purchase quantity up
    PASS G10_magnet_memo_split: Magnet memo enforces the supplier split
    PASS G11_air_freight_active: Air freight memo can make international shipment on time
    PASS G12_air_freight_expired: Expired air-freight memo no longer shortens lead time
    PASS G13_sustainability_preference: Comparable offers prefer environmental rating A
    PASS G14_strategic_preference: Strategic supplier kept within the 12% savings band
    PASS G15_hazmat_review: Hazardous material is flagged for review
    PASS G16_manager_approval_over_40k: Order over $40k requires Sourcing Manager approval
    PASS G17_vp_approval_over_120k: Order over $120k requires VP of Supply Chain approval

17 of 17 cases passed.
