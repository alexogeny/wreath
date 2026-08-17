# Sign a final artifact

Use `wreath.provenance` when approval must survive beyond the HTTP request that
created a report, export, evidence bundle, or other immutable artifact. RFC 9421
HTTP Message Signatures authenticate a message in flight; a provenance sidecar
authenticates exact bytes at rest.

```python
from wreath.provenance import Provenance, ProvenanceKey

reviewer = ProvenanceKey("reviewer", reviewer_public, sign=reviewer_hsm.sign)
approver = ProvenanceKey("approver", approver_public, sign=approver_hsm.sign)

approval = Provenance.for_artifact(
    report,
    name="final-report.pdf",
    media_type="application/pdf",
    quorum=2,
)
approval = approval.countersign(report, reviewer)
approval = approval.countersign(report, approver)

await objects.write("final-report.pdf", report)
await objects.write("final-report.pdf.provenance", approval.dump())
```

To verify later, load both objects and provide the trusted public-key map:

```python
approval = Provenance.load(await objects.read("final-report.pdf.provenance"))
report = await objects.read("final-report.pdf")
verified = approval.verify(report, trusted_public_keys)
```

`ArtifactChanged` means the bytes no longer match. `InvalidProvenance` names a
missing key, bad signature, altered chain, or unmet quorum. A verifier cannot
lower the quorum stored in the signed envelope; it may require a higher one.

Each added signature covers the digest, artifact metadata, quorum, and all
earlier signatures. Removing or reordering a prior approval therefore breaks
every later counter-signature. Signing may be a callback into an HSM or KMS;
`ProvenanceKey.from_seed` is the dependency-free local/test form.

Reference: [`wreath.provenance`](../reference/provenance.md).
