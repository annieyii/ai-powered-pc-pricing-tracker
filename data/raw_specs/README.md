# Raw specification text

**The `<sku>.txt` files are not part of the current repository tree.**

One file per SKU, named `<sku>.txt`, holds the product title followed by the
Specifications block copied verbatim from that SKU's Best Buy product page.
They are the input to `tracker/extract.py` and the evidence its grounding
validator checks against: a value the model reports that never appeared in the
file is reported as ungrounded.

Publishing them would mean redistributing Best Buy page content, which the site
terms restrict. The written explanation submitted with this repository says
collection was kept manual for that reason, and shipping the same content here
would contradict it. So the files are kept locally and excluded from version
control. They were tracked earlier in this repository's history and commits
before the removal still contain them; this removes them going forward rather
than rewriting a published history. The extraction code, the hand-verified `products.csv`, and the
field-by-field `extraction_review.<model>.csv` are all present, so the method
and its results can still be read; only the copied page text is absent.

To reproduce an extraction run, recreate `<sku>.txt` from the `source_url` in
`products.csv`. Verbatim means verbatim: no reordering, no unit tidying, no
dropping of rows that look irrelevant. Editing a file to "help" the extraction
would remove the only evidence the grounding check has.

`tracker/extract.py` prints `nothing extracted; no usable raw spec files` when
the directory is empty. Nothing else reads it: the dashboard does not, and the
price path does not.
