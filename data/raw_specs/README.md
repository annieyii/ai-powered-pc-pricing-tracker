# Raw specification text

One file per SKU, named `<sku>.txt`, holding the product title followed by the
Specifications block copied verbatim from that SKU's Best Buy product page.
Verbatim means verbatim: no reordering, no unit tidying, no dropping of rows
that look irrelevant.

These files are the input to `tracker/extract.py`. They are also what the
grounding validator checks against, so a value the model reports that never
appeared in the file here is reported as ungrounded. Editing a file to "help"
the extraction would remove the only evidence that check has.

Nothing else reads this directory. The dashboard does not, and the price path
does not.
