# ContextLab v2 experiment charter

Status: preregistered foundation contract

Owner: Kevin Araujo

Implementation and evidence custodian: Codex

Current execution note, 2026-08-07: G0 through G4 have exact-hash human approvals. G2 and G3 both
retain the simple baseline. F3 and F5 are approved only as `accepted-negative`, no-promotion
demonstrations. The G4 approval binds one historical static viewer snapshot. Later viewer edits make
the current program barrier fail closed against that old asset binding, as intended. The portfolio
Story is a separate release layer with its own verification and release approval; it is not a new G4
approval. Historical planning and pending-state language below remains part of the frozen contract.

## Thesis and scope

ContextLab tests this thesis:

> Enterprise AI quality depends on selecting and compiling the smallest current, authoritative,
> and sufficient evidence set. Retrieval and memory must be tested as observable policies.

The core study compares five final strategy lanes over exactly 160 task IDs. Each lane runs with
`low` and `high` reasoning using `deepseek/deepseek-v4-flash-0731` through the DeepSeek provider
route on OpenRouter. The final benchmark contains 120 static or retrieval tasks and 40 temporal or
memory tasks. The experiment tests retrieval, context construction, temporal state, answer quality,
cost, and latency. It does not test general intelligence, production uptime, or performance outside
the frozen NovaLearn corpus.

## Primary research questions

1. Which retrieval stages improve required-evidence recall and final answer quality at a fixed
   context budget?
2. When do lexical, semantic, hierarchical, or structured routes help specific task families?
3. Which memory policy improves correction, supersession, expiry, and as-of answers without harming
   the static suite?
4. What quality, token, latency, and cost trade-offs result from `low` versus `high` reasoning?
5. Do the two blind AI reviewers agree with Kevin, the sole human reviewer, closely enough to
   support a ranking claim?

## Frozen controls

Within a comparison, the corpus snapshot, task IDs, answer prompt, generator model and provider,
reasoning effort, evidence-token budget, output-token limit, temperature, trial count, and grading
contract are fixed. A run manifest hashes those values before any call. Candidate retrieval and
context-pack construction are measured separately from generation. Every failure remains in the
run ledger.

The four v1 strategies remain controls: full context, semantic RAG, compiled wiki, and text-to-SQL.
A v2 candidate changes one declared retrieval or memory variable at a time until an ablation gate
supports combining stages.

## Evaluation boundary

The task split is fixed at 64 regression, 32 judge-calibration, 48 sealed-capability, and 16
showcase tasks. The repository contains only the public identity and stratum of each sealed task.
Kevin keeps sealed questions when needed, expected answers, gold evidence, and grading labels in an
external bundle. The sealed evaluator may return task and candidate hashes, grades, permitted
failure labels, and aggregate metadata only.

Every final answer cell is reviewed independently by GPT-5.6 Sol high, Claude Opus 5 medium, and
Kevin. Strategy and reasoning identities are blind. Each reviewer receives all 1,600 unique cells,
one shared calibration packet, and preregistered hidden repeats. No cell enters an aggregate without
Kevin's recorded grade. Ordinal results use the median; binary and categorical results use the
majority. Individual grades and disagreement remain visible.

## Prohibited leakage paths

The system under test must not read or infer protected truth through any of these paths:

- evaluator-only or external sealed files;
- expected answers, scoring notes, gold source IDs, or grading labels in prompts;
- task filenames, source order, document metadata, IDs, or lexical markers that encode a label;
- traces, memory entries, review packets, viewer exports, caches, indexes, or generated summaries;
- previous judge output, Kevin's grades, hidden-repeat mappings, or blind identity maps;
- environment variables, logs, shell history, error text, or Git objects that contain credentials.

Adapters use an approved corpus boundary. Protected paths, parent traversal, and symlink escapes are
rejected. A sealed import rejects unknown fields instead of silently dropping them. Development
reports may use returned sealed grades, but never raw sealed truth.

## Acceptance and stop rules

An experiment is promoted only when its preregistered primary metric improves on the target family,
the paired distribution and confidence interval support that direction, full-suite regressions stay
inside the declared limit, and cost and latency stay inside budget. Trace evidence must show a
plausible evidence-path improvement.

Stop and preserve a negative result when any of these conditions occurs:

- a gate acceptance item fails;
- a protected or sealed datum reaches a system-under-test input;
- a run cannot reproduce its manifest, adapter, prompt, corpus, model route, or saved hashes;
- OpenRouter reports that the key's non-resetting US$15.00 limit is exhausted or unavailable;
- the requested model, DeepSeek provider route, or `low`/`high` reasoning factor cannot be honored;
- reviewer calibration exposes an ambiguous rubric item; all three reviews then restart from the
  same corrected frozen packet;
- reviewer disagreement is too high or materially strategy-dependent for a ranking claim;
- a memory configuration fails provenance, replay, rollback, or static-regression requirements.

A provider failure is recorded. It is not silently rerouted. A failed experimental feature remains
in the report and is not presented as promoted.

The local ledger reports an informational warning at US$12.00. It does not stop calls or replace
OpenRouter's authoritative, non-resetting US$15.00 key limit.

## Claims and authority

Kevin alone approves gates, public claims, the review rubric, sealed evaluation, and the final
narrative. Codex implements and integrates the study. GPT-5.6 Sol high and Claude Opus 5 medium are
independent model reviewers; neither approves its own claim. ContextLab reports that the study has
one human reviewer and does not claim that AI reviewers are better than human reviewers unless a
future experiment measures that claim.
