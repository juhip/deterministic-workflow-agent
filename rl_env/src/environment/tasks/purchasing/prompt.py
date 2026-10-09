"""The instructions the student gets. The policy itself is only in the PDFs."""

from pathlib import Path
from textwrap import dedent


def instructions(database: Path, documents: Path, use_hints: bool) -> str:
    text = dedent(f"""
        You are the purchasing planner at Harbor Fabrication Co., a manufacturer.

        The planning database is {database} (SQLite). It holds the production orders and
        their need-by dates, bills of materials, stock on hand, existing purchase orders,
        suppliers and their catalog offers. The planning date is
        `scenario_config.current_date`. The company's purchasing policy and the management
        memos are PDFs in {documents}.

        Plan the purchases for every open production order and write them into the
        database:

        1. Work out what each production order is short of, after stock on hand and open
           purchase orders, and by when it is needed.
        2. Choose suppliers and quantities as the policy and the memos in force on the
           planning date require.
        3. Insert one row into `purchase_orders` per order you place:
           - `po_number`: a new number, such as AGT-0001
           - `component_id`, `supplier_id`, `quantity`
           - `unit_price`: the supplier's catalog price
           - `order_date`: the planning date
           - `expected_delivery_date`: when the order will arrive, as YYYY-MM-DD
           - `rationale`: must begin with exactly `RELEASED automatically` if the order
             can go to the supplier now, or with exactly
             `DRAFT, awaiting approval by <approver> before release` if a person must
             approve it first, naming the approver the policy requires. Then say why.
        4. Insert a row into `alerts` for every problem a planner must know about, such
           as a production order that will be late.

        Do not change or delete any existing row in any table, and do not change the
        tables' structure. Commit your changes. Your work is graded from the database
        file alone when you finish.
        """).strip()
    if use_hints:
        text += (
            "\n\n"
            + dedent("""
            Hints:
            - `python` has `pypdf` installed for reading the PDFs, and the `sqlite3` module.
            - Memos may name parts by a different ID than the database does; match them
              by description.
            - Check every rule against the planning date: memos start and end.
            """).strip()
        )
    return text
