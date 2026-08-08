# Public release checklist

This checklist prepares one exact release packet. No step in the final publication section is
authorized until Kevin approves that packet.

## Research integrity

- [x] The public narrative matches [Claims](CLAIMS.md) word for word where it reports a result.
- [x] Every displayed metric has an artifact path, exact JSON pointer, source-run identity, raw
  SHA-256, and gate commitment.
- [x] G2 is `retain-simple`; no advanced retriever is described as promoted.
- [x] G3 is descriptive `retain-simple`; no memory policy is described as promoted or generally
  harmful.
- [x] F3 and F5 are `accepted-negative`, no-promotion demonstrations.
- [x] Kevin is identified as the sole human reviewer.
- [x] Failed runs, failed-entry decisions, negative results, and calibration limits remain visible.
- [x] No canonical experiment output, protected truth, sealed data, gold label, evaluator-only
  record, prior approval, or rejected historical artifact changed.

**Complete when:** every public result can be traced to the current approved gate chain and no claim
exceeds its recorded scope.

## Story and laboratory

- [x] Story is the default entry point.
- [x] Explore the lab preserves the detailed analytical viewer and its deep links.
- [x] The architecture shows corpus and events, strategy adapters, candidate evidence, context packs,
  provider gateway, review and gates, evidence export, and the viewer.
- [x] The sealed evaluator is visibly outside the public system boundary.
- [x] One saved run shows retrieval, context construction, generation, citations, review, and gate
  decision.
- [x] The time control shows supersession without deleting prior state.
- [x] The negative promotion decision is easy to understand without reading raw JSON.
- [x] Keyboard navigation, readable contrast, mobile layout, and reduced motion pass browser review.
- [x] The production viewer makes no model call and has no fallback sample values.

**Complete when:** a first-time visitor can explain the problem, method, result, Kevin's role, and
limits in five minutes, while a technical reader can open the evidence lineage.

## Curated bundle

- [x] The bundle is generated from an explicit allowlist into a clean destination.
- [x] The normal clone is below 100 MB.
- [x] No ordinary tracked file is larger than 10 MB unless the final packet names and justifies it.
- [x] The large legacy G2 viewer projection is absent. Its rewritten bytes do not preserve the
  embedded semantic commitment.
- [x] A compact G2 projection preserves lineage to the approved G2 artifact and has its own verified
  manifest entry.
- [x] The public TCC target is
  [`media/ContextLab_TCC_v1.pdf`](media/ContextLab_TCC_v1.pdf), not the private source PDF. It is
  labelled as the frozen Portuguese v1 manuscript, with corrected metadata and unchanged visible
  content.
- [x] The release manifest lists every exported file, byte size, and SHA-256.
- [x] A second export from a clean checkout reproduces the same manifest.
- [x] The bundle builds and runs without the private evidence vault.
- [x] All Markdown links inside the bundle resolve.
- [x] No public file contains a credential, secret, private absolute path, private grade, protected
  truth, sealed content, evaluator-only content, or unpublished reviewer data.

**Complete when:** the deterministic export reproduces, passes all scans, stays inside the size
limits, and runs independently.

## Local verification

Run the complete Python suite from the repository root:

```sh
PYTHONPATH=evaluation/v2 python3 -m unittest discover \
  -s evaluation/v2/tests -p 'test_*.py'
```

Run the complete viewer check from `viewer/`:

```sh
npm run check
```

- [x] Python exits zero with no failure, error, or new skipped test.
- [x] Viewer lint, type checking, tests, and production build exit zero.
- [x] Viewer test count is not below the recorded baseline.
- [x] No test was deleted, skipped, or weakened to hide a regression.
- [x] Secret scan reports zero findings.
- [x] Protected-data scan reports zero findings.
- [x] Private-path scan reports zero findings.
- [x] Every displayed metric resolves to its declared artifact, pointer, and hash.
- [x] The historical G4 approval is described only as an exact old-snapshot binding. The current
  program barrier remains fail-closed against later viewer edits.
- [x] The portfolio Story passes its separate release verification and is not described as a new G4
  approval.

**Complete when:** the saved command logs, exit codes, test counts, scan reports, and browser evidence
are ready for the final release packet.

## Media

- [x] Hero view is verified at desktop and mobile dimensions.
- [x] Architecture view is verified at desktop and mobile dimensions.
- [x] Strategy comparison view is verified at desktop and mobile dimensions.
- [x] Time-machine view is verified at desktop and mobile dimensions.
- [x] Run-replay view is verified at desktop and mobile dimensions.
- [x] Social poster has readable text, safe crop margins, and an accurate claim.
- [x] Walkthrough runs for 60 to 90 seconds and follows
  [the script](WALKTHROUGH_SCRIPT.md).
- [x] Walkthrough captions are burned in or always visible and also exist as a separate VTT or SRT
  file.
- [x] The walkthrough explains the problem, controls, negative result, evidence viewer, Kevin's role,
  and limits.
- [x] No cloned voice is used. An existing approved voice can be used only if it is already available
  and authorized.

**Complete when:** all media renders at the target dimensions, captions match the final edit, and no
frame exposes private or unsupported content.

## Final release packet

- [x] The packet records the exact current commit and clean working-tree proof.
- [x] It records Python and viewer verification summaries.
- [x] It records the current F3 and F5 pending-record hashes, AI review status, approval artifacts,
  final gates, and precise authorized claims.
- [x] It proposes the public repository name and URL.
- [x] It recommends, but does not apply, the license.
- [x] It lists the repository description, topics, homepage, deployment target, and URL.
- [x] It proposes the `portfolio-v1` tag without creating it.
- [x] It records bundle size, file count, largest files, and all scan results.
- [x] It links the local preview, screenshots, poster, video, captions, case study, claim ledger, and
  release manifest.
- [x] It lists every external action that approval will authorize.
- [x] The F3 and F5 source or pending hashes have not changed since Kevin's recorded approvals.

**Complete when:** Kevin can decide from this one file without an intermediate approval request.

## Publication after approval

Run these steps only after Kevin approves the exact final packet:

- [ ] Reconfirm the current F3 and F5 approval bindings.
- [ ] Rerun every verification and scan.
- [ ] Create and push the curated public repository.
- [ ] Apply the approved license, description, homepage, and topics.
- [ ] Deploy the approved build.
- [ ] Create and push the approved `portfolio-v1` tag.
- [ ] Publish the approved screenshots, poster, walkthrough, and captions.
- [ ] Verify every public URL in a fresh browser session.
- [ ] Add final public links to the private research README.
- [ ] Commit and push the authorized private-repository updates.
- [ ] Return the public URLs, exact commits, tag, test summaries, and approved public claims.

**Complete when:** all approved external actions are live, verified, and recorded, and both working
trees are clean.
