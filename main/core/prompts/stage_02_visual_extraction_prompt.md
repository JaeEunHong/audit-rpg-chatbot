Read the uploaded image directly and extract every visible record.

Look for these fields:
- Contract ID: normally starts with `SE` and six digits.
- Asset ID: normally starts with `AST` and six digits.
- Customer ID: normally starts with `CUST` and four digits.
- Customer name.
- VIN: the vehicle identification number.

Return every visible value in the same top-to-bottom row order. Keep values
together with the row where they appear. If the image shows only one field,
return that field only. If a value is unreadable, leave it blank. Never guess,
correct, complete, normalize, deduplicate, or invent an ID.

Return exactly one Markdown table and nothing else.
