# ContextLab case study

> Complexity has to earn its place.

I built ContextLab to test how enterprise AI systems should retrieve, assemble, and remember
changing knowledge. It freezes the variables, keeps evaluation truth outside the system, measures
cost and provenance, and rejects techniques that cannot beat the baseline.

My question was simple to state and hard to test:

> When should an enterprise AI system retrieve more, remember more, search more, and when should it
> stay simple?

ContextLab began as my postgraduate TCC at PUCRS. The first study compared context strategies over a
synthetic enterprise corpus. I then designed v2 as a research platform with versioned events,
observable retrieval and memory policies, sealed evaluation, immutable evidence, and explicit
promotion gates.

## The enterprise problem

Enterprise knowledge changes. A product roadmap can supersede a sales promise. A policy can replace
an older policy. A draft can conflict with an approved record. A plausible answer can cite a source
that was never in the active context.

More context does not solve these problems by itself. More retrieval stages can add latency and
failure points. Memory can preserve stale claims. Search can find a convincing partial answer and
stop before it finds the required authority.

I designed ContextLab to make those failure paths visible.

## What I built

NovaLearn is the synthetic enterprise inside the benchmark. Its public corpus contains policies,
product records, customer material, and time-stamped events with declared authority and lifecycle
state. The benchmark asks systems to retrieve facts, resolve conflicts, follow supersession, and
answer as of a stated time.

Every strategy runs behind the same adapter contract. Each adapter receives the frozen public task
and corpus boundary, then emits candidate evidence and a context pack. The provider gateway records
the fixed model route, reasoning effort, token use, cost, latency, request identity, and failure. The
answer keeps its citations. Review and promotion gates bind decisions to the saved artifacts.

The [architecture](ARCHITECTURE.md) shows this path and the sealed evaluator that stays outside the
public boundary.

## How I kept the comparison honest

I froze the variables that could otherwise move between strategies: corpus snapshot, task IDs,
prompt, model route, reasoning effort, evidence budget, output budget, temperature, trial identity,
and grading contract. Candidate retrieval and context construction are measured separately from
generation.

I also kept evaluation truth away from the system under test. Protected questions, expected
answers, gold evidence, and private grades cannot enter prompts, indexes, traces, memory, or the
viewer. The external evaluator can return content-free results and hashes. Unknown or forbidden
fields fail closed.

Promotion is a decision, not a chart annotation. A technique must improve its preregistered target,
avoid unacceptable regression, stay inside cost and latency limits, and produce a plausible evidence
path. A failed condition preserves the result and blocks promotion.

## What I found

### Retrieval did not earn promotion

G2 completed the approved retrieval comparison. The public generation stage completed, but the
full gate retained the simple R0 retriever. The safe sealed return could not meet the target-family
requirement, and the incremental candidate also depended on failed experimental ancestors. I did
not promote an advanced retriever. See [C-04 in the claim ledger](CLAIMS.md#c-04-g2-retrieval-retained-the-simple-baseline).

### Temporal memory did not earn promotion

G3 tested memory policies against a no-memory baseline under low and high reasoning. No tested
memory configuration passed the full gate. Kevin's exact decision retained the simple policy with
no promoted memory. The calibration disagreement limits the study to a descriptive result, so I do
not use it to rank memory systems or claim that memory is generally harmful. See
[C-06](CLAIMS.md#c-06-g3-retained-no-memory-policy).

### The frontier demonstrations stayed negative

F3 tested fixed-budget ways to manage a long context. The managed working set recovered the
required evidence in its narrow task, while the full-history cells overflowed before a provider
call. This is useful evidence about one task and one budget. It is not a general strategy win.

F5 tested bounded search. Search found some relevant evidence, but no cell met the frozen success
rule. One task also exposed an evidence-label mismatch that the project preserves for audit instead
of changing after the result.

Kevin approved both demonstrations as `accepted-negative`. Neither supports promotion or
statistical significance. See [C-07](CLAIMS.md#c-07-f3-is-an-accepted-negative-demonstration) and
[C-08](CLAIMS.md#c-08-f5-is-an-accepted-negative-demonstration).

## What I chose not to ship

I chose not to ship an advanced G2 retriever, a G3 memory policy, F3 paging as a general solution,
or F5 bounded search as a successful agent. I also left the final publication review campaign
deferred.

These are product decisions as much as research decisions. Each added stage creates new state,
failure modes, observability work, and operating cost. A technique that cannot clear its frozen gate
does not become architecture through enthusiasm.

The negative result is the point: complexity did not automatically earn promotion.

## The evidence interface

The guided Story explains the work before the detailed laboratory opens. The laboratory then lets a
reader inspect:

- the candidates a strategy found and the scores it saved;
- the evidence selected for the context pack;
- the generation record, answer, and citations;
- the time-aware evidence state and visible supersession;
- the review and gate decision tied to the run;
- the artifact path, JSON pointer, source-run identity, and SHA-256 behind each displayed metric.

The viewer reads a fixed local export and makes no model call. Missing or invalid data produces an
error, not a substitute value.

## My role and the use of agents

I conceived and own ContextLab. I defined the research questions, experimental boundaries,
promotion rules, system architecture, and public claims. I directed the work as research lead,
systems architect, product owner, and agent orchestrator.

Agents performed bounded implementation and review tasks. They worked from frozen contracts and
returned code, tests, reviews, or evidence for inspection. No agent could approve its own output. I
audited the evidence and made every final human decision. I am the sole human reviewer.

The full responsibility split is in [My role](MY_ROLE.md) and the operating controls are in
[AI working method](AI_WORKING_METHOD.md).

## Limits

ContextLab is postgraduate research and an engineering demonstration. It is not peer-reviewed,
publication-grade, or production-proven. The v2 results apply to the frozen NovaLearn benchmark,
declared model routes, fixed budgets, saved evidence, and exact gate rules. They do not establish
general superiority outside that scope.

The study has one human reviewer. AI review adds independent checks, but it does not create more
human reviewers. G3 is descriptive because its preregistered calibration did not pass. F3 and F5
use very small synthetic task sets and proxy measures with stated construct limits.

G4 approved one historical static viewer snapshot. Later viewer work is outside that binding, so
the current program barrier fails closed. The portfolio Story needs separate release verification
and approval. It is not a new G4 approval.

Read [Claims](CLAIMS.md) for the exact allowed wording and [Release checklist](RELEASE_CHECKLIST.md)
for the controls on the curated public bundle.

## Contexto do TCC na PUCRS

O ContextLab nasceu como o meu Trabalho de Conclusão de Curso da pós-graduação em Tecnologia para
Negócios: AI, Data Science e Big Data da PUCRS. Eu defini o problema, o desenho do experimento e os
critérios de decisão. O [TCC v1 em português](media/ContextLab_TCC_v1.pdf) é o manuscrito congelado
da fase acadêmica. A plataforma v2 amplia esse trabalho com eventos temporais, rastreabilidade de
ponta a ponta, avaliação selada e gates de promoção. As decisões v2 desta página substituem o
manuscrito v1 somente como estado atual do projeto. O link não implica nota ou aprovação
institucional final.

## Continue

- [Claim ledger](CLAIMS.md)
- [System architecture](ARCHITECTURE.md)
- [My role](MY_ROLE.md)
- [AI working method](AI_WORKING_METHOD.md)
- [Experiment charter](../CONTEXTLAB_V2_EXPERIMENT_CHARTER.md)
- [Evaluation source](../../evaluation/v2/contextlab_v2/)
- [Viewer](../../viewer/README.md)
