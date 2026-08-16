# Wreath client-facts data formats

`country.wgd` uses WGD2. Its nine-byte header contains the magic, country-table
length, IPv4 range count, and IPv6 range count. Two-byte country codes follow.
Each sorted range is encoded as an unsigned-varint gap from the preceding range,
an unsigned-varint inclusive span, and a one-byte country index. IPv6 records
represent exact runs of `/64` blocks, keeping the format portable and compact.

The native reader expands those records into operation-owned arrays once. A
256-entry first-byte directory bounds each lookup to one slice, then binary
search resolves the exact range. Misses stay unknown. The encoded image is
limited to 20,000 bytes.

`user_agent.wua` uses WUA1. It contains a deduplicated result-string table and a
compact product-token table. The native reader owns both the image and its hash
index; lookup scans the input once and materializes only its result tuple. The
encoded image is limited to 5,000 bytes.

`tools/generate_client_facts_data.py` rebuilds both images from normalized
country-range inputs and the declarative User-Agent vocabulary.
