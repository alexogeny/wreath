# `wreath.provenance`

Sign and later verify stored artifact bytes. This is deliberately separate from
[`wreath.signatures`](signatures.md): RFC 9421 authenticates an HTTP message,
while provenance binds an immutable sidecar to bytes at rest.

```python
from wreath.provenance import Provenance, ProvenanceKey

reviewer = ProvenanceKey.from_seed("reviewer", reviewer_seed)
approver = ProvenanceKey.from_seed("approver", approver_seed)

sidecar = (
    Provenance.for_artifact(report, name="report.pdf", quorum=2)
    .countersign(report, reviewer)
    .countersign(report, approver)
)
await store.write("report.pdf.provenance", sidecar.dump())

loaded = Provenance.load(await store.read("report.pdf.provenance"))
loaded.verify(report, {
    "reviewer": reviewer.public,
    "approver": approver.public,
})
```

Each signer approves the artifact digest, metadata, signed quorum, and complete
preceding signature chain. Production private keys can remain in an HSM or KMS:
construct `ProvenanceKey(key_id, public, sign=callback)`. Verification-only
processes use `ProvenanceKey.verifier` or pass raw 32-byte public keys.

::: wreath.provenance
