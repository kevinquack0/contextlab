# Public claim ledger

This file is the source of truth for public ContextLab wording. A public metric is allowed only when
its entry below names the claim status, narrow scope, artifact path, exact JSON pointer, raw file
SHA-256, and the semantic commitment used by the gate. A raw file hash covers the bytes in Git. An
`artifact_sha256` or technical-record hash covers the canonical JSON content defined by that
artifact's schema.

Source result files can retain an older pending-state string because evidence is immutable. The
current final gate, not that historical string, controls the public status.

## C-01: Kevin built and owns ContextLab end to end

**Status:** Public governance fact.

**Allowed wording:** Kevin Araujo conceived, designed, built, ran, analyzed, documented, and
presented ContextLab. He owns the complete research and engineering project, including its research
question, benchmark, method, architecture, implementation, interface, evidence, and public claims.
AI systems were tools inside his workflow, not separate project owners.

**Scope:** End-to-end project authorship, implementation ownership, and v2 governance. Kevin is also
the sole human reviewer, which is a study limit and not the extent of his contribution.

**Binding:**

- Artifact: [`docs/CONTEXTLAB_V2_EXPERIMENT_CHARTER.md`](../CONTEXTLAB_V2_EXPERIMENT_CHARTER.md)
- Pointer: Markdown fields `Owner` and `# Claims and authority`
- Raw file SHA-256: `e69a021a4afba14c3bcb5cb6b19ef927fc0e6deb8cff09e801a03567744e5eaa`

## C-02: ContextLab is a PUCRS postgraduate TCC project

**Status:** Public academic-context fact.

**Allowed wording:** ContextLab began as Kevin Araujo's postgraduate TCC in Tecnologia para
Negócios: AI, Data Science e Big Data at PUCRS. The v2 platform is a later research and engineering
expansion.

**Scope:** Academic provenance. The manuscript covers the v1 study and must not be used as evidence
for a v2 gate claim. The v2 decisions supersede it for current project status. The public link does
not imply a grade or final institutional approval.

**Binding:**

- Artifact: [`docs/portfolio/media/ContextLab_TCC_v1.pdf`](media/ContextLab_TCC_v1.pdf)
- Pointer: PDF cover and abstract; JSON pointer not applicable
- Raw file SHA-256: `bf3efd964c0370fda2c7b37e2208f1775b8c5ac16b09b72af9f28d8eb2369864`

## C-03: The v2 benchmark contract contains 160 task identities

**Status:** Approved foundation claim.

**Allowed wording:** The G1 foundation freezes 160 task identities and keeps the sealed evaluator
outside the system under test.

**Scope:** Task allocation and evaluation boundary. This count does not mean that the deferred final
review campaign is complete.

**Binding:**

- Technical artifact: [`results/v2/gates/G1.json`](../../results/v2/gates/G1.json)
- JSON pointers: `/evidence/task_count`, `/evidence/protected_adapter_boundary`,
  `/evidence/sealed_import/forbidden_gold_fixture`, `/evidence/secret_scan/findings`
- Raw file SHA-256: `f89b68192108b4e8350d700c25f6a1a79fdb413ecea5ed9b273c45791320303a`
- Canonical artifact SHA-256: `84cb75a42556251ceacb96cc36fe9f257c304336e8fdb7224bb73ab2f1f0f5ea`
- Technical evidence SHA-256: `5d7480819030d7a1c6a8e371a227e56f9d57ef122e94856c61f09f603a0f3773`
- Approval artifact: [`results/v2/gates/G1.approval.json`](../../results/v2/gates/G1.approval.json)
- Approval JSON pointers: `/status`, `/technical_evidence_sha256`,
  `/technical_gate_artifact_sha256`
- Approval raw file SHA-256: `23413cc58eb44ebd69d17b84cad3f2d592fcac28c6351161a77459cb6c212522`
- Approval canonical artifact SHA-256: `e614c5e5b4f6a3dc61396da044d003e961f0237f36ed9b7d22d82753facaefac`

## C-04: G2 retrieval retained the simple baseline

**Status:** Approved `retain-simple` result.

**Allowed wording:** G2 recorded 1,344 completed public generation cells, but the complete gate
retained R0 and promoted no advanced retriever.

**Scope:** Frozen G2 NovaLearn retrieval experiment. Public answer screening is not a semantic
correctness grade. The safe sealed return lacked the preregistered target-family aggregate, and the
incremental candidate had failed ancestors. This is not a general claim that advanced retrieval is
worse.

**Binding:**

- Gate artifact: [`results/v2/gates/G2.json`](../../results/v2/gates/G2.json)
- JSON pointers: `/stages/public_generation/completed_cell_count`, `/final_decision`,
  `/retained_retriever_id`, `/promoted_retriever_id`,
  `/stages/sealed_evaluation/incremental_candidates/0/criteria/target_family_minimum_met`,
  `/stages/sealed_evaluation/incremental_candidates/0/failed_ancestor_blockers`
- Raw file SHA-256: `d121050caab8fe38688035e88071cab467d53c831e0414ff808c1ea26b89aed2`
- Canonical artifact SHA-256: `fab112609d373c54315b199f9d299448725f343d16d86798f309458feef6e3b6`
- Technical record SHA-256: `47939addde8fcf68fa2a51de9c42627606e1a27fb81884a17496fbf97d436911`
- Approval artifact: [`results/v2/gates/G2.approval.json`](../../results/v2/gates/G2.approval.json)
- Approval JSON pointers: `/decision`, `/gate_sha256`, `/reviewer_role`
- Approval raw file SHA-256: `0f52edeec723daf948ce9ab6d4ac7c85b885d9d9cde9e6bd0d38902fe17e239b`
- Approval gate commitment: `47939addde8fcf68fa2a51de9c42627606e1a27fb81884a17496fbf97d436911`

## C-05: G3 preserved a complete public grid with failures

**Status:** Approved descriptive evidence input.

**Allowed wording:** The G3 public run records 1,120 cells: 1,118 completed generations and two
preserved failures.

**Scope:** Public G3 generation grid only. This count is not a panel ranking or a publication claim.

**Binding:**

- Source artifact: [`results/v2/memory/g3_public_generation_run.json`](../../results/v2/memory/g3_public_generation_run.json)
- JSON pointers: `/recorded_cell_count`, `/generation_status_counts/completed`,
  `/generation_status_counts/failed`
- Source raw file SHA-256: `38026a40099bf1523062cabca2100acde06550ddc7eee6ce4c7eb787d8178464`
- Source canonical artifact SHA-256: `4d106a96bac6a5375c0810e8acd02377bfe9014091e890e8c7f92ca28a8e985c`
- Approved gate artifact: [`results/v2/gates/G3.json`](../../results/v2/gates/G3.json)
- Gate JSON pointer that binds the source: `/public_run_sha256`
- Gate raw file SHA-256: `a0b1f91671782a83311f72392ea53e71a08c50bda16a157bd2d5c77ce1b4a29b`
- Gate canonical artifact SHA-256: `39f70576e33d9341d9585a3f981873f6743cd8ef0dbb4dfddb24496b18fe0788`

## C-06: G3 retained no memory policy

**Status:** Approved descriptive `retain-simple` result.

**Allowed wording:** No tested M1 through M4 configuration was eligible for promotion. Kevin
retained the simple baseline and promoted no memory policy.

**Scope:** Frozen NovaLearn temporal-memory experiment. The preregistered calibration did not pass,
so this result is descriptive. It cannot rank memory systems and cannot support a claim that memory
is universally harmful.

**Binding:**

- Gate artifact: [`results/v2/gates/G3.json`](../../results/v2/gates/G3.json)
- JSON pointers: `/eligible_configurations`, `/eligible_policies`, `/final_decision`,
  `/promoted_memory_policy`, `/acceptance_checks/three_member_panel_calibration`, `/limitations/4`
- Raw file SHA-256: `a0b1f91671782a83311f72392ea53e71a08c50bda16a157bd2d5c77ce1b4a29b`
- Canonical artifact SHA-256: `39f70576e33d9341d9585a3f981873f6743cd8ef0dbb4dfddb24496b18fe0788`
- Technical record SHA-256: `8ec5a6c951097a56f510d4091eaab58ad35689b1a9a8b4fe0e17ad6c6879460b`
- Human decision: [`results/v2/reviews/g3/kevin/final-gate-decision.json`](../../results/v2/reviews/g3/kevin/final-gate-decision.json)
- Decision JSON pointers: `/decision`, `/selected_policy`, `/pending_gate_artifact_sha256`,
  `/technical_record_sha256`, `/reviewer_role`
- Decision raw file SHA-256: `c0b154b1290b9bf26675f83afbd33e3a93bf64d0287cce8720dfb9cfcfe5e7e4`
- Decision canonical artifact SHA-256: `8e29b04856ceb9e240de7920c76816ffa11e0da01f871067dbc5d525ce66fdd1`

## C-07: F3 is an accepted-negative demonstration

**Status:** Approved `accepted-negative`; no promotion.

**Allowed wording:** This 40-cell public demonstration records mean answer quality and
recovered-evidence recall of 1.000 and 1.000 for managed working set, 0.525 and 0.500 for dense
retrieval, and 0.500 and 0.000 for episodic memory. All ten full-history cells overflowed at the
same 13,000-token budget.

**Scope:** One synthetic task under the frozen strategy, reasoning, and temperature-zero repeat
contract. `answer_quality` is an identifier-mention proxy. The repeats are not independent task
samples. Full history produced no answer. These results do not establish general superiority or
statistical significance.

**Binding:**

- Technical claim: [`results/v2/frontier/f3/reviews-attempt-04/technical.json`](../../results/v2/frontier/f3/reviews-attempt-04/technical.json)
- Technical JSON pointer: `/claim`
- Technical raw file SHA-256: `2641e6f8ba5cdcbbb7d8d54f9a52a1a2e7797f8e4e38c14585e27e3193e6ad91`
- Technical canonical artifact SHA-256: `f18f7eb214a0d9c268ba028771127bd9ff068fd6454455b646cc8343ec42aebe`
- Technical record SHA-256: `e54d607bb5c2481c0ce935bfe8410f491fa063a756b1a3c3e44c6471db8acf0e`
- Source result: [`results/v2/frontier/f3/virtual_context_paging.attempt-06.final.json`](../../results/v2/frontier/f3/virtual_context_paging.attempt-06.final.json)
- Source JSON pointers: `/artifact_sha256`, `/task_id`, `/trial_ids`, `/cells`
- Source raw file SHA-256: `3e0bfd6833aa3732723f7d85eb9a296b650980f49b261f1a5edd44769814b819`
- Source canonical artifact SHA-256: `84068a095877ad745dc1ec31ee8c130a4d542a7e1abe57402ab043de1c96b613`
- Pending gate: [`results/v2/frontier/f3/reviews-attempt-04/pending.json`](../../results/v2/frontier/f3/reviews-attempt-04/pending.json)
- Pending JSON pointers: `/artifact_sha256`, `/proposed_decision`, `/final_status`,
  `/ai_reviews/0/decision`, `/ai_reviews/1/decision`
- Pending raw file SHA-256: `e3de75210c9542a0f6809ab22bd86bf8736f318b44addc12d3b3d1c37a2b4ec9`
- Pending canonical artifact SHA-256: `ccf3685c5e16bac59806fccdce2a1092d8ba0cfb47ddbd99b6aecbba2a95eae9`
- Kevin approval: [`results/v2/frontier/f3/reviews-attempt-04/kevin.approval.json`](../../results/v2/frontier/f3/reviews-attempt-04/kevin.approval.json)
- Approval JSON pointers: `/decision`, `/pending_gate_artifact_sha256`, `/reviewer_role`,
  `/technical_record_sha256`
- Kevin approval raw file SHA-256: `355f97a22bea402403c9944127b17c6dbed31c45199883d411a174c5d324182f`
- Kevin approval canonical artifact SHA-256: `869be1e1158de1c616728be6170fb4f6ca84d60eeedae6c5b9ad50f7504559b9`
- Final gate: [`results/v2/frontier/f3/reviews-attempt-04/final.json`](../../results/v2/frontier/f3/reviews-attempt-04/final.json)
- Final JSON pointers: `/final_status`, `/human_approval/decision`,
  `/human_approval/approval_artifact_sha256`, `/technical_record_sha256`
- Final raw file SHA-256: `81bc0ad40a828888df5430d8bc56fe56359fc17bab6fdac4c2c1ec12e2646595`
- Final canonical artifact SHA-256: `d82232f114b83718eada843b8b18fe270cc1d611c11b87f21c3fcd0efdd202a5`

## C-08: F5 is an accepted-negative demonstration

**Status:** Approved `accepted-negative`; no promotion.

**Allowed wording:** This eight-cell bounded-search demonstration records mean evidence coverage of
0.14375 and bounded-search `task_success` of 0.00. The saved comparator accepted-proxy rates are
0.25 for R5 and 0.00 for R6, with mean evidence coverage of 0.05 and 0.00.

**Scope:** Two synthetic tasks and a fixed bounded-search contract. The comparator proxy is not a
correctness grade. Coverage is identifier based. The result has an evidence-label mismatch, a
legacy field alias, and no semantic human grade for the answers. It does not establish statistical
significance.

**Binding:**

- Technical claim: [`results/v2/frontier/f5/reviews-attempt-04/technical.json`](../../results/v2/frontier/f5/reviews-attempt-04/technical.json)
- Technical JSON pointer: `/claim`
- Technical raw file SHA-256: `37d3eb6d66da4ddf785b62910c6c7b5811ff4b0dcf8e44962d9b1e6527a6315f`
- Technical canonical artifact SHA-256: `b3efcecbe3973b564605848497c9da8f5f49af4ee231f6dbcee1d8be57f13872`
- Technical record SHA-256: `62ea0884b22fc9e474a5abc17cc8ce4ad2c355d9eb8db3194b377adaa6b980f3`
- Source result: [`results/v2/frontier/f5/bounded_search.final.json`](../../results/v2/frontier/f5/bounded_search.final.json)
- Source JSON pointers: `/artifact_sha256`, `/task_ids`, `/trial_ids`,
  `/aggregate/bounded_search/mean_evidence_coverage`,
  `/aggregate/bounded_search/task_success_rate`, `/aggregate/R5/task_success_rate`,
  `/aggregate/R5/mean_evidence_coverage`, `/aggregate/R6/task_success_rate`,
  `/aggregate/R6/mean_evidence_coverage`
- Source raw file SHA-256: `e917ca44cc44249a46690bcf551a6a798769c73c510517754f63326a1b553b1d`
- Source canonical artifact SHA-256: `aa0b68ab988836da3c52825fbd5e27d385528b741b3aa0bf1bc470910bd82377`
- Pending gate: [`results/v2/frontier/f5/reviews-attempt-04/pending.json`](../../results/v2/frontier/f5/reviews-attempt-04/pending.json)
- Pending JSON pointers: `/artifact_sha256`, `/proposed_decision`, `/final_status`,
  `/ai_reviews/0/decision`, `/ai_reviews/1/decision`
- Pending raw file SHA-256: `276aeaba6514a55557a03f38e2ea99559c5a570605de89710913af86113d0473`
- Pending canonical artifact SHA-256: `cb0fb47f6d38f04f31face4c2b5f682f6e3f118f45d2322266a07fde7d20ca54`
- Kevin approval: [`results/v2/frontier/f5/reviews-attempt-04/kevin.approval.json`](../../results/v2/frontier/f5/reviews-attempt-04/kevin.approval.json)
- Approval JSON pointers: `/decision`, `/pending_gate_artifact_sha256`, `/reviewer_role`,
  `/technical_record_sha256`
- Kevin approval raw file SHA-256: `767b29266101571cc5ffb083ca7a31ae06e3d4ac21515ba5d162d2f38457d8a4`
- Kevin approval canonical artifact SHA-256: `e06eb54ba3bf210c53777224c3e0bb6098cac8f7c74c958ba18038c0df8755ca`
- Final gate: [`results/v2/frontier/f5/reviews-attempt-04/final.json`](../../results/v2/frontier/f5/reviews-attempt-04/final.json)
- Final JSON pointers: `/final_status`, `/human_approval/decision`,
  `/human_approval/approval_artifact_sha256`, `/technical_record_sha256`
- Final raw file SHA-256: `f1850c8438cda8606ec9eb601bcd500663b292ec11316d072afc5910825736ec`
- Final canonical artifact SHA-256: `9b8b2c4f6430017c94230e956b92270143c4f106ee9e87a84e4ca427dcb3a81f`

## C-09: Kevin is the sole human reviewer

**Status:** Approved governance and limitation claim.

**Allowed wording:** Kevin is the sole human reviewer. AI reviewers supplied independent bounded
reviews, but no AI reviewer had final authority and no agent could approve its own output.

**Scope:** v2 gate governance. "Human-audited" is allowed only when the same text states that Kevin
is the sole human reviewer.

**Binding:**

- Human decision artifact: [`results/v2/reviews/g3/kevin/final-gate-decision.json`](../../results/v2/reviews/g3/kevin/final-gate-decision.json)
- JSON pointers: `/reviewer`, `/reviewer_role`, `/decision`
- Raw file SHA-256: `c0b154b1290b9bf26675f83afbd33e3a93bf64d0287cce8720dfb9cfcfe5e7e4`
- Canonical artifact SHA-256: `8e29b04856ceb9e240de7920c76816ffa11e0da01f871067dbc5d525ce66fdd1`
- F3 approval pointer: [`/reviewer_role`](../../results/v2/frontier/f3/reviews-attempt-04/kevin.approval.json)
- F3 approval raw file SHA-256: `355f97a22bea402403c9944127b17c6dbed31c45199883d411a174c5d324182f`
- F5 approval pointer: [`/reviewer_role`](../../results/v2/frontier/f5/reviews-attempt-04/kevin.approval.json)
- F5 approval raw file SHA-256: `767b29266101571cc5ffb083ca7a31ae06e3d4ac21515ba5d162d2f38457d8a4`

## C-10: G4 approved one historical viewer snapshot

**Status:** Historical exact-asset approval. It does not approve the current portfolio release.

**Allowed wording:** G4 approved the exact static viewer export and assets recorded in its gate.
Later viewer edits are outside that binding, so the current program barrier correctly fails closed.
The portfolio Story is a separate release layer with separate verification and release approval.

**Scope:** Historical G4 asset identity only. Do not present the current Story, current production
build, or new public bundle as G4-approved.

**Binding:**

- Gate artifact: [`results/v2/gates/G4.json`](../../results/v2/gates/G4.json)
- JSON pointers: `/final_decision`, `/viewer_export_sha256`,
  `/verification/static_assets_sha256`, `/verification/static_asset_count`
- Raw file SHA-256: `b93bb93bb2e04b1b67c2847d58496d84f28251b3663cbefeaa25740f1c9a21e9`
- Canonical artifact SHA-256: `78bbedd2dc3404ee1ac57ec58d48710d05a03e8a833663e992bdd4f9281a2d33`
- Approval artifact: [`results/v2/gates/G4.approval.json`](../../results/v2/gates/G4.approval.json)
- Approval JSON pointers: `/decision`, `/pending_gate_artifact_sha256`,
  `/technical_record_sha256`, `/reviewer_role`
- Approval raw file SHA-256: `2cccaa287165e4ff572b84770ca8e891ee3dd9636b60de96bb0285dde4d0f2ef`
- Approval canonical artifact SHA-256: `3b5db8d45a880a545096f8100f35d04e862653a7e60ae2bd8c82ffd57f6f3ed9`

## Claim boundaries

These statements are always required:

- ContextLab is not peer-reviewed, publication-grade, or production-proven.
- Results apply to the frozen NovaLearn benchmark and the recorded controls.
- No approved result proves general superiority outside that scope.
- G3 does not prove that memory is universally harmful.
- F3 and F5 are demonstrations, not significance results.
- The study has one human reviewer: Kevin Araujo.
- Failed runs, negative results, failed-entry decisions, and historical rejected artifacts remain
  visible.

The [experiment charter](../CONTEXTLAB_V2_EXPERIMENT_CHARTER.md) defines the complete research
boundary. Its raw file SHA-256 is
`e69a021a4afba14c3bcb5cb6b19ef927fc0e6deb8cff09e801a03567744e5eaa`.
