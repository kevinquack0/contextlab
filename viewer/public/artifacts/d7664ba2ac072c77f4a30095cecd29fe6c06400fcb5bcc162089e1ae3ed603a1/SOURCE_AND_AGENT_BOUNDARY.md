# Source and agent boundary

ContextLab separates research evidence from agent planning. This rule prevents an implementation
note, review opinion, or generated summary from becoming experimental truth.

## Primary evidence

Primary evidence comes from the frozen NovaLearn corpus, saved experiment runs, public evaluator
outputs, promotion gates, and Kevin's exact-hash decisions. Public metrics must resolve to these
artifacts through a JSON pointer and SHA-256 commitment.

## Agent planning material

Agents used plans, task contracts, implementation notes, and review findings to do bounded work.
This material could guide a change or identify a defect. It could not change a result, approve a
gate, or support a public metric by itself.

## Authority

Kevin Araujo defined the research questions, system boundaries, promotion rules, and public claims.
Agents implemented and reviewed assigned work. No agent could approve its own output. Kevin audited
the evidence and made every final human decision as the sole human reviewer.

This boundary is part of the system design: evidence can influence a decision only through the
declared gate, and complexity must earn promotion against the frozen baseline.
