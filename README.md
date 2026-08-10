# ContextLab

> Complexity has to earn its place.

I built ContextLab to test how enterprise AI systems should retrieve, assemble, and remember
changing knowledge. It freezes the variables, keeps evaluation truth outside the system, measures
cost and provenance, and rejects techniques that cannot beat the baseline.

The project began as my postgraduate TCC at PUCRS. It grew into a controlled research and
engineering platform for one practical question:

> When should an enterprise AI system retrieve more, remember more, search more, and when should it
> stay simple?

ContextLab uses NovaLearn, a synthetic enterprise with policies, product records, sales claims, and
events that can conflict or become stale. Strategy adapters receive the same frozen task and corpus
controls. They produce inspectable context packs. A fixed provider gateway records generation, cost,
latency, and citations. Promotion gates then decide whether added machinery earned its place.

The answer was often no.

- G2 completed its approved retrieval study and retained the simple R0 retriever. No advanced
  retriever was promoted.
- G3 completed its descriptive temporal-memory study and retained no memory policy. This result is
  specific to the frozen NovaLearn benchmark; it is not a claim that memory is generally harmful.
- F3 virtual-context paging and F5 bounded search are approved as `accepted-negative`
  demonstrations. Neither result supports promotion or a significance claim.

The [claim ledger](docs/portfolio/CLAIMS.md) binds each public result to an artifact, an exact JSON
pointer, a raw file SHA-256, and the relevant semantic commitment.

## Public release

- [Open the live case study](https://contextlab-research.vercel.app/).
- [Browse the curated public repository](https://github.com/kevinquack0/contextlab).
- [Inspect the immutable `portfolio-v1` release snapshot](https://github.com/kevinquack0/contextlab/tree/portfolio-v1).
- [Review the successful public verification run](https://github.com/kevinquack0/contextlab/actions/runs/31245108231).

The public repository is a generated, allowlisted release. The private TCC repository remains the
evidence vault. Public commit `f2af44e956ca8251c51790482e5e7f8e33210047` is the exact commit
referenced by `portfolio-v1`.

## Start here

- [Read the case study](docs/portfolio/CASE_STUDY.md) for the five-minute story.
- [Watch the 75-second captioned walkthrough](docs/portfolio/media/contextlab-walkthrough.mp4) or
  read its [WebVTT captions](docs/portfolio/media/contextlab-walkthrough.vtt).
- [Open the architecture](docs/portfolio/ARCHITECTURE.md) to see the public and sealed boundaries.
- [Review my role](docs/portfolio/MY_ROLE.md) and the [AI working method](docs/portfolio/AI_WORKING_METHOD.md).
- [Explore the laboratory](viewer/README.md) for question comparison, retrieval traces, temporal
  evidence, strategy matrices, and run replay.
- [Read the experiment charter](docs/CONTEXTLAB_V2_EXPERIMENT_CHARTER.md) and browse the
  [v2 source](evaluation/v2/contextlab_v2/).
- [Read the frozen Portuguese v1 TCC manuscript](docs/portfolio/media/ContextLab_TCC_v1.pdf). The v2
  gate decisions in this portfolio supersede it for current status. The link does not imply a grade
  or final institutional approval.
- [Open the media kit](docs/portfolio/media/README.md) for the five verified screenshots, poster,
  walkthrough, and accessible captions.

## What the viewer shows

The React and Vite viewer reads a fixed local export. It does not call a model, accept a remote data
override, or insert sample results when data is missing. Casual visitors can follow the guided
story. Technical readers can open the laboratory, inspect context construction, follow citations,
and replay saved evidence paths.

The sealed evaluator stays outside the public system. It can return content-free grades, permitted
failure labels, aggregates, and cryptographic commitments. It cannot expose protected questions,
expected answers, gold evidence, or private review data to the system under test or the viewer.

## My role

I conceived, designed, built, ran, analyzed, documented, and presented ContextLab. The research
question, benchmark, evaluation method, software architecture, implementation, interface, evidence
audit, and public narrative are all my work. I used AI systems as tools during implementation and
review, just as I used Python and React. They did not own a separate part of the project. I am
responsible for the complete research and engineering system and made every final decision.

## Limits

ContextLab is a postgraduate research project and an engineering demonstration. It is not
peer-reviewed, publication-grade, or production-proven. Its approved v2 conclusions apply to the
frozen NovaLearn benchmark, fixed model routes, fixed budgets, saved provider evidence, and stated
gate rules. The work preserves failed runs, failed-entry decisions, negative results, and calibration
limits.

G4 approved one historical static viewer snapshot. Later viewer edits make the current program
barrier fail closed against that old asset binding. The portfolio Story is a separately verified
release layer, not a new G4 approval.

The private research workspace remains the evidence vault. A deterministic allowlist produces the
smaller public release. One exact final release packet governs the public repository, deployment,
license, release tag, and media. Those external actions can run only under that approval.

## Run the checks locally

From the repository root, run the full provider-free Python verification:

```sh
PYTHONPATH=evaluation/v2 python3 -m contextlab_v2 --help

PYTHONPATH=evaluation/v2 python3 -m unittest discover \
  -s evaluation/v2/tests -p 'test_*.py'
```

Run the viewer locally and then run its complete check:

```sh
cd viewer
npm ci
npm run dev

# In a second shell, from viewer/
npm run check
```

`npm run check` runs lint, type checking, tests, and the production build. The private evidence
vault keeps experiment-specific replay commands. The public checks above make no paid provider call.

## Portuguese context

O ContextLab nasceu como o meu Trabalho de Conclusão de Curso da pós-graduação em Tecnologia para
Negócios: AI, Data Science e Big Data da PUCRS. Eu defini o problema de pesquisa, os limites do
experimento e os critérios de decisão. A versão pública apresenta o método, o sistema e os resultados
com limites claros. O TCC original registra o estudo acadêmico v1; a plataforma v2 amplia esse
trabalho com rastreabilidade, memória temporal, gates de promoção e um visualizador de evidências.
