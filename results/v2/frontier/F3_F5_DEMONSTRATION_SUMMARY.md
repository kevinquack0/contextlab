# ContextLab v2 F3/F5 results memo

## Status and scope

This is a public-evidence demonstration. It is not a significance result and does not promote a
retrieval or context strategy.

The saved F3 and F5 result bytes did not change during this follow-up. Kevin previously approved
their canonical artifact hashes. The current result reviews use `gpt-5.6-sol-high` and
`gpt-5.6-terra-high`. Claude was unavailable because the local subscription OAuth token was not
available. The fallback reviewers saw the public result workspace and result evidence, but no
implementation source.

Both current AI review pairs passed with no findings. Kevin approved both exact result-gate hashes
on 2026-08-08 at 03:00:21 UTC:

| Experiment | Review attempt | AI review | Approved pending record | Approval artifact | Final decision artifact | State |
| --- | --- | --- | --- | --- | --- | --- |
| F3 | `reviews-attempt-04` | Sol pass; Terra pass | `ccf3685c5e16bac59806fccdce2a1092d8ba0cfb47ddbd99b6aecbba2a95eae9` | `869be1e1158de1c616728be6170fb4f6ca84d60eeedae6c5b9ad50f7504559b9` | `d82232f114b83718eada843b8b18fe270cc1d611c11b87f21c3fcd0efdd202a5` | `accepted-negative` |
| F5 | `reviews-attempt-04` | Sol pass; Terra pass | `cb0fb47f6d38f04f31face4c2b5f682f6e3f118f45d2322266a07fde7d20ca54` | `e06eb54ba3bf210c53777224c3e0bb6098cac8f7c74c958ba18038c0df8755ca` | `9b8b2c4f6430017c94230e956b92270143c4f106ee9e87a84e4ca427dcb3a81f` | `accepted-negative` |

Failed earlier reviews are preserved. They exposed inaccurate metric language, a legacy comparator
label, and ambiguous hash instructions. The current claims and review contract correct those
problems without changing an experiment result.

## Artifact identity

`artifact_sha256` is the repository's canonical JSON commitment. The raw file SHA-256 is listed
separately.

| Experiment | Final public result | Canonical artifact SHA-256 | Raw file SHA-256 |
| --- | --- | --- | --- |
| F3 | `results/v2/frontier/f3/virtual_context_paging.attempt-06.final.json` | `84068a095877ad745dc1ec31ee8c130a4d542a7e1abe57402ab043de1c96b613` | `3e0bfd6833aa3732723f7d85eb9a296b650980f49b261f1a5edd44769814b819` |
| F5 | `results/v2/frontier/f5/bounded_search.final.json` | `aa0b68ab988836da3c52825fbd5e27d385528b741b3aa0bf1bc470910bd82377` | `e917ca44cc44249a46690bcf551a6a798769c73c510517754f63326a1b553b1d` |

F3 has one legacy naming exception. The nested score references call their raw-file commitment
`artifact_sha256`; the linked score file separately carries its canonical `record_sha256`. Both
commitments replay. This is a schema-name limitation, not missing score evidence.

## Main results

### F3: virtual-context paging

F3 contains 40 cells: four strategies, two reasoning efforts, and five temperature-zero provider
repeat samples.

| Strategy | Cells completed | Mean `answer_quality` | Mean recovered-evidence recall | Active tokens |
| --- | ---: | ---: | ---: | ---: |
| Managed working set | 10/10 | 1.000 | 1.000 | 11,110 |
| Dense retrieval | 10/10 | 0.525 | 0.500 | 12,955 |
| Episodic memory | 10/10 | 0.500 | 0.000 | 511 |
| Full history | 0/10 | unavailable | unavailable | preparation overflow |

The 30 provider-backed cells used 386,130 prompt tokens, 88,584 completion tokens, and 54,403
reasoning tokens. Returned billed cost was exactly `$0.027343476`. The ten full-history preparation
failures used zero provider tokens and cost `$0`.

These numbers show that the managed working set recovered all four required evidence identifiers in
this one fixed task and budget. They do not establish general superiority. The `answer_quality`
field is also only an identifier-mention proxy, as explained below.

### F5: bounded search

F5 contains eight bounded-search cells over S025 and S026. It used 16 provider turns and nine tool
calls.

| Strategy | Cells | Mean evidence coverage | Outcome field | Outcome rate | Tool calls | Verifier failures | Cost |
| --- | ---: | ---: | --- | ---: | ---: | ---: | ---: |
| Bounded search | 8 | 0.14375 | bounded `task_success` | 0.00 | 9 | 10 | `$0.003222756` |
| R5 saved comparator | 4 | 0.05 | `accepted_proxy` | 0.25 | 0 | 3 | `$0.0035754096` |
| R6 saved comparator | 4 | 0.00 | `accepted_proxy` | 0.00 | 0 | 4 | `$0.00420196` |

The R5/R6 values are saved historical comparator costs and proxy outcomes. They were not incurred in
this follow-up. The F5 v1 result schema transports `accepted_proxy` in a field named
`task_success`; it is not a semantic correctness grade and must not be compared as if it were the
same outcome as bounded-search success.

Bounded search used 29,884 prompt tokens, 7,647 completion tokens, and 6,111 reasoning tokens. Its
returned billed cost was exactly `$0.003222756`. Combined original F3 and F5 result cost was
`$0.030566232`.

## Why F5 coverage improved but success stayed at zero

Bounded search required a terminal answer, complete required-source identifier coverage, and no
verifier failure. No cell met that all-or-nothing rule. Seven cells returned answers but cited only a
subset of the required source identifiers. One cell stopped before an answer because another turn
would have exceeded the 8,000-token cumulative bound.

The cell coverage values were:

- S025: `0.00`, `0.20`, `0.20`, `0.00`; mean `0.10`.
- S026: `0.25`, `0.00`, `0.25`, `0.25`; mean `0.1875`.

Across cells, 31 of 36 required source slots were missing from answers. The weighted cited-source
coverage was therefore 5/36, or about 0.1389. The published 0.14375 is the preregistered unweighted
mean of the eight per-cell ratios.

### Missed evidence and identifier mismatch

S025 requires `NL-005`, `NL-007`, `NL-008`, `NL-014`, and `NL-023`. Three searches instead returned
`NL-009`, `NL-015`, and `NL-014`. `NL-015` directly states the Corpus Readiness Checklist asked for
by the question, but the frozen S025 required set omits it. Only `NL-014` counted. The failed S025
cell later retrieved `NL-023`, but it had no terminal answer and therefore received zero coverage.

This is a material task-evidence mismatch: retrieval found direct checklist evidence that the label
does not accept. The old task must remain immutable, but a future versioned task should audit whether
`NL-015` is required or accepted alternative evidence.

S026 requires `NL-005`, `NL-007`, `NL-008`, and `NL-024`. Three cells read `NL-023`, which is not in
that set. One search retrieved `NL-005`. No cell retrieved the full set, and no cell retrieved
`NL-024`.

### Retrieval problems

- Seven cells used one retrieval call and then answered. The failed cell used two. None used the
  allowed four calls.
- Generic lexical searches found the most direct wording in `NL-015`, but not the complete frozen
  evidence set.
- Three S026 cells read the lower-authority `NL-023` sales proposal instead of the required policy,
  reliability, implementation, and support sources.
- The transcript grows after every tool result. Large source reads consumed enough context to make
  another provider turn impossible in one cell.
- The agent had no explicit retrieval-coverage plan and stopped after finding a plausible answer.

### Answer problems

- The seven answers are often plausible, but plausibility was not enough for the frozen coverage
  requirement.
- S025 answers centered on the directly relevant `NL-015` checklist and therefore failed the label
  even when they answered the question well.
- Two S026 answers mention `NL-007` after reading `NL-023`, whose text refers to `NL-007`. They did
  not retrieve the raw `NL-007` source. The identifier-only verifier still awarded 0.25 coverage.
- Answers omitted most required sources and did not separate directly retrieved support from a
  source mentioned inside another source.
- One cell produced no terminal answer.

### Verifier problems

The bounded verifier scans the answer text for required document identifiers. It does not require
that an identifier was retrieved, that a section citation is present, that the cited text supports
the claim, or that the answer is semantically correct. `task_success` is only “answer exists, every
required identifier appears, and no recorded failure.”

The saved baseline adapter has a second limitation: it maps G2 `accepted_proxy` into the F5 field
named `task_success`. That legacy alias caused an earlier review to reject the claim. This memo and
the current technical claim report it as an accepted-proxy rate. The immutable F5 artifact retains
its v1 field name.

### Bound-related failure

S025/high/trial-02 used two calls and 4,814 provider prompt-plus-completion tokens. Before turn 3,
the saved transcript, estimated next prompt, and fixed 3,000-token completion allowance would have
exceeded the 8,000-token cumulative limit. The harness correctly recorded:

- `cumulative_token_limit_would_be_exceeded`;
- `no_terminal_answer`; and
- `required_source_coverage_incomplete`.

There were no wall-time, dead-end, or tool-call-limit failures. The token-bound failure is valid and
must not be removed by increasing the bound after seeing the result.

## Why episodic memory scored 0.500 quality and 0.000 recall

The episodic strategy activated only two prior-answer pages:

- `public/f3/episode/g3-s026-enterprise-rollout`;
- `public/f3/episode/g3-s030-teams-status`.

Together they used 511 tokens. Both episode pages have an empty `evidence_ids` list, and no raw page
was active. Recovered-evidence recall is computed from active page metadata, so zero of the four
required raw identifiers (`NL-007`, `NL-014`, `NL-024`, `NL-027`) were recovered. The `0.000` recall
is therefore correct.

The field named `answer_quality` is computed differently. It checks only whether the answer text
mentions each required document identifier. Every episodic answer repeated `NL-014` and `NL-027`
from the two prior answers, but not `NL-007` or `NL-024`, so every cell scored 2/4 = `0.500`.

Findings:

- Unsupported knowledge: material claims are supported only indirectly by prior generated answers,
  not by active raw pages as the task instruction requires.
- Missing citations: the answers print section-like citations, but those raw sections were not in
  active context.
- Retrieval failure: yes; the episodic strategy recovered no raw required evidence.
- Identifier mismatch: no; the required IDs and regex matching agree.
- Scoring bug: there is no arithmetic or replay bug. There is a construct and naming limitation:
  `answer_quality` measures required-ID mentions, not semantic quality or citation support.

The 0.500/0.000 combination is therefore possible without contradiction. It demonstrates that prior
answers can carry source identifiers and claims while raw-evidence recovery remains zero.

## Full-history overflow finding

The full public history required 33,741 estimated tokens. The fixed budget was 13,000 tokens, so the
deterministic preparation overflow was 20,741 tokens. The same prepared context was used for five
trial identities and two reasoning efforts. All ten cells failed before a provider call, with zero
usage, zero latency, and zero cost.

These are valid equal-budget outcomes. They show that full history was unavailable under the frozen
budget. They are not ten independent observations of model quality; they are ten planned cells that
share one deterministic structural overflow. The 13,000-token budget must remain unchanged.

## Calls, costs, and review audit

This follow-up made no paid experiment call. Both experiment replays used saved provider evidence,
and the result cost and usage stayed exact.

The result-review workflow completed 12 local Codex reviewer invocations across preserved attempts:
eight passes and four useful failures. Review usage totaled 24,433,035 input tokens, of which
23,123,968 were cached, plus 148,749 output tokens and 63,566 reasoning-output tokens. Reviewer
receipts do not contain a monetary cost, and review compute is not included in experiment cost.
Claude failed availability checks before a review invocation, so it has no fabricated receipt or
usage record.

## Replay commands

From the repository root:

```sh
PYTHONPATH=evaluation/v2 python3 -m contextlab_v2 run-frontier-f3

PYTHONPATH=evaluation/v2 python3 -m contextlab_v2 finalize-frontier-f3 \
  --input results/v2/frontier/f3/completions.attempt-06.json

PYTHONPATH=evaluation/v2 python3 -m contextlab_v2 run-frontier-f5

PYTHONPATH=evaluation/v2 python3 -m contextlab_v2 gate-frontier-result \
  --experiment F3
PYTHONPATH=evaluation/v2 python3 -m contextlab_v2 gate-frontier-result \
  --experiment F5
```

These commands verify and reuse saved evidence. They do not make a paid call while the saved
provider artifacts and generation specifications match.

## Verification

- The complete Python suite passed: 567 tests in 341.403 seconds.
- The focused F3/F5 review and replay tests passed before gating; a post-approval review-focused run
  passed 78 tests in 21.516 seconds.
- Both saved-evidence result replays reproduced the approved F3 and F5 canonical artifact hashes.
- The viewer passed lint, TypeScript checks, 20 Vitest tests, and its production build. No viewer
  adapter was needed.
- Repository and viewer secret scans passed with zero repository findings.
- Recomputed public-review containment passed with exact bytes: F3 has 3,491 files and 108,798,457
  bytes; F5 has 3,416 files and 108,404,859 bytes. Neither current reviewer workspace contains
  implementation code or a forbidden path.

## Limitations

- F3 uses one synthetic task. F5 uses two synthetic tasks.
- Temperature-zero repeats are planned provider-repeat samples, not independent task samples.
- Full history did not produce an answer, so F3 has no full-history quality comparison.
- F3 `answer_quality` is an identifier-mention proxy.
- F5 evidence coverage is also identifier based and can count an identifier that was not directly
  retrieved.
- F5 has a frozen S025 evidence-label mismatch and a legacy accepted-proxy/task-success alias.
- There is no semantic human grade for the eight F5 answers in this follow-up.
- The result review checks integrity and claim discipline. It does not convert the demonstrations
  into significance results.
- No viewer adapter was added. The existing viewer is a frozen G4 export; the Markdown memo and
  public JSON results are sufficient for this follow-up.

## Smallest useful next experiment

First run a provider-free, versioned audit over the same eight F5 transcripts. Report four separate
fields: required sources directly retrieved, required sources mentioned in the answer, citations
grounded in retrieved chunks, and semantic answer correctness. Relabel the saved R5/R6 comparator as
`accepted_proxy`, and adjudicate the S025 role of `NL-015` without changing the old task.

Only after that audit should a new provider run be considered. The smallest useful confirmation is
two cells: one S025 cell and one S026 cell, one fixed effort and one trial each, with the same model,
temperature, tools, and 8,000-token bound. Change only the agent instruction to require a retrieval
plan and grounded citations before answering. Do not run a larger campaign until those two cells
show that the corrected metrics and evidence labels behave as intended.
