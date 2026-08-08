# AI working method

ContextLab uses AI agents as bounded workers and reviewers. Kevin owns the research and makes the
final human decisions. The method keeps implementation, independent review, and approval as
different acts.

## Authority

| Role | Can do | Cannot do |
| --- | --- | --- |
| Kevin Araujo | Define scope, change the research contract, accept claims, approve gates, approve release | Add a second human reviewer by description alone |
| Implementation agent | Change assigned code or documents, run checks, produce technical evidence | Approve its own work or alter canonical evidence |
| Independent AI reviewer | Inspect a byte-bound public workspace and return findings or a verdict | Create human approval, see protected truth, or rewrite the result |
| Sealed evaluator | Grade against protected truth and return the allowed content-free fields | Expose protected questions, answers, gold evidence, or private review data |

Kevin is the sole human reviewer. AI review adds checks. It does not add human reviewers.

## Working sequence

### 1. Kevin defines the decision

Kevin states the research question, changed variable, fixed controls, target metric, promotion rule,
cost and latency bounds, stop conditions, and allowed public claim.

**Complete when:** the experiment can pass, fail, or stop without inventing a rule after seeing the
result.

### 2. An agent receives a bounded task

The task names owned files, inputs, exclusions, test commands, return evidence, and destructive or
external actions that need separate authority. The worker knows that other work can exist in the
same repository and must preserve it.

**Complete when:** the worker can state what it owns, what it must preserve, and how success will be
checked.

### 3. The worker implements and verifies

The worker changes only the assigned surface, runs the required focused checks, and records failures.
Canonical experiment outputs, protected data, prior approvals, and rejected history remain
unchanged.

**Complete when:** the assigned checks pass, the diff contains only in-scope changes, and every known
failure is reported.

### 4. Independent review inspects fixed bytes

The review workspace contains the exact public artifact and the narrow material needed to evaluate
it. It excludes implementation source when the review contract requires result-only inspection. A
review receipt binds the model identity, invocation, input commitment, output, and finding status.

**Complete when:** each required reviewer has a real receipt, all blocking findings have a recorded
disposition, and the reviewed bytes still match the pending gate.

### 5. Kevin audits the evidence

Kevin checks the result, failures, limitations, review findings, and proposed public wording. His
approval names the exact technical or pending-gate hash. Approval is create-only.

**Complete when:** Kevin records an exact-hash decision or leaves the gate unapproved. Silence is not
approval.

### 6. Integration replays the gate

The supervisor integrates accepted work, reruns the complete verification, checks the working tree,
and confirms that no source or gate hash drifted. A changed binding blocks progress.

**Complete when:** the full local checks pass and the current bytes match every approval used by the
release.

### 7. Publication gets one separate decision

The final release packet lists the exact commit, tests, claims, bundle manifest, scans, media,
repository metadata, license proposal, deployment target, tag, and external actions. Kevin approves
or changes that complete packet once.

**Complete when:** approval covers the exact packet. Only then can the approved external actions
run.

## Fail-closed rules

- A changed artifact loses the old approval.
- A missing receipt is missing evidence, not an implied pass.
- A failed provider call remains a failed call.
- A protected-data finding stops the public release.
- A result that misses a promotion rule stays negative.
- A reviewer cannot approve its own output.
- A historical gate cannot approve a later release layer.

The final rule matters for the current portfolio. G4 approved one exact historical static viewer
snapshot. Later viewer edits make the current program barrier fail closed against that binding. The
portfolio Story has separate verification and release approval; it is not a new G4 approval.

## How agents contributed

Agents performed bounded implementation, evidence checks, testing, documentation, interface work,
and independent result review. Kevin directed the tasks, reviewed the returned work, reconciled
conflicts, and decided what could be claimed or released.

For the current F3 and F5 result gates, GPT-5.6 Sol high and GPT-5.6 Terra high reviewed the frozen
public result workspaces. Both review pairs passed. Kevin then approved the exact pending records as
`accepted-negative`. This process approved narrow negative demonstrations. It did not promote the
techniques or create significance results.

## Audit trail

- [Experiment charter](../CONTEXTLAB_V2_EXPERIMENT_CHARTER.md)
- [My role](MY_ROLE.md)
- [G3 human decision](../../results/v2/reviews/g3/kevin/final-gate-decision.json)
- [F3 final result gate](../../results/v2/frontier/f3/reviews-attempt-04/final.json)
- [F5 final result gate](../../results/v2/frontier/f5/reviews-attempt-04/final.json)
- [Public claim ledger](CLAIMS.md)
