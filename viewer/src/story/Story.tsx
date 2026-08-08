import { useState, type KeyboardEvent } from 'react';
import ArrowRight from '@carbon/icons-react/es/ArrowRight';

import { BoundGateLabel, BoundValue, EvidenceBinding } from './EvidenceBinding';
import { formatEvidenceValue, getEvidence, publicArtifactHref } from './evidence';
import { storyLinks } from './links';

export type StoryLabView = 'comparison' | 'methods' | 'replay';

interface StoryProps {
  onOpenLab: (view: StoryLabView) => void;
}

const architectureNodes = [
  {
    name: 'Corpus and events',
    detail: 'Public knowledge and ordered change records enter through a frozen snapshot.',
  },
  {
    name: 'Strategy adapters',
    detail: 'Each retrieval or memory policy implements the same external contract.',
  },
  {
    name: 'Context packs',
    detail: 'Selected evidence is ordered, budgeted, and bound to source identifiers.',
  },
  {
    name: 'Provider gateway',
    detail: 'The same request envelope records model, cost, latency, and raw output.',
  },
  {
    name: 'Grading and gates',
    detail: 'Frozen promotion rules can retain the baseline even when a component improves.',
  },
  {
    name: 'Evidence viewer',
    detail: 'Public projections expose runs, citations, commitments, and decisions.',
  },
] as const;

const traceStages = [
  {
    id: 'candidates',
    label: 'Candidate retrieval',
    title: 'Both conflicting events survived retrieval.',
    body: (
      <p>
        The saved run retrieved <BoundValue id="trace.earlier_candidate" /> and{' '}
        <BoundValue id="trace.current_candidate" /> from the public event stream. The conflict was
        preserved for context construction.
      </p>
    ),
    evidenceIds: ['trace.run_id', 'trace.earlier_candidate', 'trace.current_candidate'],
  },
  {
    id: 'context',
    label: 'Context construction',
    title: 'The context pack kept the disagreement visible.',
    body: (
      <p>
        The selected public evidence occupied <BoundValue id="trace.context_tokens" suffix=" tokens" />.
        ContextLab recorded the count beside the exact run receipt.
      </p>
    ),
    evidenceIds: ['trace.context_tokens'],
  },
  {
    id: 'generation',
    label: 'Generation',
    title: 'The provider resolved the current value.',
    body: (
      <p>
        The saved execution finished with status <BoundValue id="trace.execution_status" /> and selected{' '}
        <strong><BoundValue id="temporal.current_value" /></strong> as the current audience.
      </p>
    ),
    evidenceIds: ['trace.execution_status', 'temporal.current_value'],
  },
  {
    id: 'citations',
    label: 'Citations',
    title: 'The answer points to both sides of the conflict.',
    body: (
      <p>
        The public receipt links the earlier claim, <BoundValue id="temporal.earlier_value" />, and the
        later higher-authority claim, <BoundValue id="temporal.current_value" />.
      </p>
    ),
    evidenceIds: ['temporal.earlier_value', 'temporal.current_value'],
  },
  {
    id: 'review',
    label: 'Review',
    title: 'Review checked the packet. It did not grant authority.',
    body: (
      <p>
        Independent AI gate reviews reached <BoundValue id="g3.review_status" />. Those reviews could
        report findings, but no agent could approve the result.
      </p>
    ),
    evidenceIds: ['g3.review_status'],
  },
  {
    id: 'decision',
    label: 'Gate decision',
    title: 'Kevin retained simple memory.',
    body: (
      <p>
        Kevin made the final <BoundValue id="g3.decision" /> decision as the{' '}
        <BoundValue id="g3.human_reviewer_role" />. No memory policy was promoted.
      </p>
    ),
    evidenceIds: ['g3.decision', 'g3.human_reviewer_role', 'g3.promoted_memory_policy'],
  },
] as const;

function StoryHeader({ onOpenLab }: StoryProps) {
  return (
    <header className="story-header">
      <div className="story-header__inner">
        <a className="story-brand" href="#story" aria-label="ContextLab Story home">
          <span aria-hidden className="story-brand__mark">CL</span>
          <span>ContextLab</span>
        </a>
        <nav aria-label="Story sections" className="story-nav">
          <a href="#question">Question</a>
          <a href="#evidence">Evidence</a>
          <a href="#role">Kevin's role</a>
          <a href="#limits">Limits</a>
        </nav>
        <a
          className="story-cta story-cta--compact"
          href="#comparison"
          onClick={() => onOpenLab('comparison')}
        >
          <span>Explore the lab</span>
          <span aria-hidden className="story-cta__icon"><ArrowRight size={16} /></span>
        </a>
      </div>
    </header>
  );
}

function DecisionField() {
  return (
    <div aria-label="Approved decision map" className="story-decision-field">
      <div className="story-decision-field__rule">
        <span>Technique</span>
        <span>Gate result</span>
      </div>
      <article>
        <h2>Retrieve</h2>
        <p>
          <BoundGateLabel id="g2.gate" /> retained <BoundValue id="g2.retained_retriever" />
        </p>
      </article>
      <article>
        <h2>Remember</h2>
        <p>
          <BoundGateLabel id="g3.gate" /> promoted no policy
        </p>
      </article>
      <article>
        <h2>Search</h2>
        <p>
          <BoundValue id="f5.experiment" /> remained an <BoundValue id="f5.final_status" /> demo
        </p>
      </article>
      <p className="story-decision-field__note">Promotion requires evidence across the full frozen gate.</p>
    </div>
  );
}

function Architecture() {
  return (
    <section aria-labelledby="architecture-title" className="story-section story-architecture" id="architecture">
      <div className="story-section__heading">
        <h2 id="architecture-title">Truth stays outside the system under test.</h2>
        <p>
          I designed the platform so strategies can change without gaining access to evaluator truth.
        </p>
      </div>
      <div className="story-architecture__frame">
        <div className="story-architecture__public">
          <p className="story-architecture__boundary-label">Public execution boundary</p>
          <ol aria-label="ContextLab system architecture" className="story-architecture__flow">
            {architectureNodes.map((node) => (
              <li key={node.name}>
                <strong>{node.name}</strong>
                <span>{node.detail}</span>
              </li>
            ))}
          </ol>
        </div>
        <aside aria-label="Sealed evaluator boundary" className="story-architecture__sealed">
          <span>Outside the public boundary</span>
          <h3>Sealed evaluator</h3>
          <p>Protected truth returns only content-free metrics and commitments.</p>
        </aside>
      </div>
    </section>
  );
}

function TemporalCase() {
  const [moment, setMoment] = useState(0);
  const isCurrent = moment === 1;
  const earlierState = isCurrent ? getEvidence('temporal.earlier_state') : null;
  const currentState = getEvidence('temporal.current_state');

  return (
    <section aria-labelledby="temporal-title" className="story-section story-temporal" id="time-case">
      <div className="story-section__heading">
        <h2 id="temporal-title">A correct answer depends on when you ask.</h2>
        <p>Move the control to reveal how later, higher-authority evidence changes the active claim.</p>
      </div>
      <div className="story-temporal__control">
        <label htmlFor="story-time-control">Knowledge state</label>
        <input
          aria-valuetext={isCurrent ? 'After supersession' : 'Before supersession'}
          id="story-time-control"
          max="1"
          min="0"
          onInput={(event) => setMoment(Number(event.currentTarget.value))}
          step="1"
          type="range"
          value={moment}
        />
        <div aria-hidden className="story-temporal__labels">
          <span>Earlier evidence</span>
          <span>Later evidence</span>
        </div>
      </div>
      <div aria-live="polite" className="story-temporal__stage" data-moment={isCurrent ? 'current' : 'earlier'}>
        <article className={isCurrent ? 'story-temporal__claim story-temporal__claim--superseded' : 'story-temporal__claim'}>
          <span>{earlierState ? formatEvidenceValue(earlierState) : 'active at this point'}</span>
          <h3><BoundValue id="temporal.earlier_value" /></h3>
          <p>Authority <BoundValue id="temporal.earlier_authority" /></p>
        </article>
        {isCurrent ? (
          <article className="story-temporal__claim story-temporal__claim--active">
            <span>{formatEvidenceValue(currentState)}</span>
            <h3><BoundValue id="temporal.current_value" /></h3>
            <p>Authority <BoundValue id="temporal.current_authority" /></p>
          </article>
        ) : (
          <div aria-hidden className="story-temporal__pending">
            Later evidence has not entered the snapshot.
          </div>
        )}
      </div>
      <EvidenceBinding
        ids={[
          'temporal.earlier_value',
          'temporal.earlier_authority',
          'temporal.current_value',
          'temporal.current_authority',
          'temporal.earlier_state',
          'temporal.current_state',
        ]}
        label="Inspect temporal evidence"
      />
    </section>
  );
}

function TraceExplorer() {
  const [activeId, setActiveId] = useState<(typeof traceStages)[number]['id']>('candidates');
  const activeIndex = traceStages.findIndex((stage) => stage.id === activeId);
  const activeStage = traceStages[activeIndex];

  function moveFocus(event: KeyboardEvent<HTMLButtonElement>, index: number): void {
    let nextIndex: number;
    if (event.key === 'ArrowRight' || event.key === 'ArrowDown') nextIndex = (index + 1) % traceStages.length;
    else if (event.key === 'ArrowLeft' || event.key === 'ArrowUp') nextIndex = (index - 1 + traceStages.length) % traceStages.length;
    else if (event.key === 'Home') nextIndex = 0;
    else if (event.key === 'End') nextIndex = traceStages.length - 1;
    else return;

    event.preventDefault();
    const next = traceStages[nextIndex];
    setActiveId(next.id);
    const tab = event.currentTarget.parentElement?.parentElement?.querySelector<HTMLButtonElement>(
      `#trace-tab-${next.id}`,
    );
    tab?.focus();
  }

  return (
    <section aria-labelledby="trace-title" className="story-section story-trace" id="evidence">
      <div className="story-section__heading">
        <h2 id="trace-title">Follow one run to its decision.</h2>
        <p>
          This public replay follows a saved temporal run. It is an evidence trace, not an evaluation score.
        </p>
      </div>
      <div className="story-trace__question">
        <span>Saved question</span>
        <p><BoundValue id="trace.question" /></p>
        <code><BoundValue id="trace.run_id" /></code>
      </div>
      <div className="story-trace__explorer">
        <div aria-label="Trace stages" className="story-trace__tabs" role="tablist">
          {traceStages.map((stage, index) => (
            <div key={stage.id}>
              <button
                aria-controls={`trace-panel-${stage.id}`}
                aria-selected={activeId === stage.id}
                id={`trace-tab-${stage.id}`}
                onClick={() => setActiveId(stage.id)}
                onKeyDown={(event) => moveFocus(event, index)}
                role="tab"
                tabIndex={activeId === stage.id ? 0 : -1}
                type="button"
              >
                {stage.label}
              </button>
            </div>
          ))}
        </div>
        <div
          aria-labelledby={`trace-tab-${activeStage.id}`}
          className="story-trace__panel"
          id={`trace-panel-${activeStage.id}`}
          role="tabpanel"
        >
          <p className="story-trace__active-label">{activeStage.label}</p>
          <h3>{activeStage.title}</h3>
          {activeStage.body}
          <EvidenceBinding ids={[...activeStage.evidenceIds]} />
        </div>
      </div>
    </section>
  );
}

function SourceLink({ evidenceId, children }: { evidenceId: string; children: string }) {
  const href = publicArtifactHref(getEvidence(evidenceId));
  if (!href) return null;
  return <a href={href}>{children}</a>;
}

export default function Story({ onOpenLab }: StoryProps) {
  return (
    <div className="story-theme" id="story">
      <a className="skip-link" href="#story-main">Skip to case study</a>
      <StoryHeader onOpenLab={onOpenLab} />
      <main id="story-main" tabIndex={-1}>
        <section aria-labelledby="story-hero-title" className="story-hero">
          <div className="story-hero__copy">
            <p className="story-eyebrow">Postgraduate research by Kevin Araujo</p>
            <h1 id="story-hero-title">Complexity has to earn its place.</h1>
            <p>I built a governed testbed for retrieval, temporal memory, and bounded search in changing enterprise knowledge.</p>
            <div className="story-hero__actions">
              <a className="story-cta" href="#problem">
                <span>Read the case</span>
                <span aria-hidden className="story-cta__icon"><ArrowRight size={16} /></span>
              </a>
              <a className="story-text-link" href="#comparison" onClick={() => onOpenLab('comparison')}>
                Explore the lab
              </a>
            </div>
          </div>
          <DecisionField />
        </section>

        <section aria-labelledby="problem-title" className="story-section story-problem" id="problem">
          <div className="story-section__heading">
            <h2 id="problem-title">Enterprise knowledge does not stand still.</h2>
            <p>A system can retrieve a plausible answer and still miss what is current, authoritative, or safe to use.</p>
          </div>
          <div className="story-problem__states">
            <article>
              <h3>Stale</h3>
              <p>An older rule remains easy to retrieve after a newer rule takes effect.</p>
            </article>
            <article>
              <h3>Conflicting</h3>
              <p>Two sources make incompatible claims with different authority.</p>
            </article>
            <article>
              <h3>Changing</h3>
              <p>The correct answer changes with event order and the requested snapshot.</p>
            </article>
          </div>
        </section>

        <section aria-labelledby="question-title" className="story-section story-question" id="question">
          <div>
            <h2 id="question-title">The research question</h2>
            <blockquote>
              When should an enterprise AI system retrieve more, remember more, search more, and when should it stay simple?
            </blockquote>
          </div>
          <aside>
            <h3>NovaLearn makes the test repeatable.</h3>
            <p>
              I designed a synthetic enterprise with policies, customer records, conflicts, and temporal events. No result in this study claims performance outside that frozen benchmark.
            </p>
          </aside>
        </section>

        <section aria-labelledby="controls-title" className="story-section story-controls">
          <div className="story-section__heading">
            <h2 id="controls-title">I froze the variables before comparing techniques.</h2>
            <p>The strategy changes. The question set, provider envelope, budgets, gate rules, and evaluator boundary do not.</p>
          </div>
          <div className="story-controls__sequence">
            <article><h3>Freeze</h3><p>Bind corpus, tasks, configuration, budget, and promotion criteria.</p></article>
            <article><h3>Run</h3><p>Save context, output, citations, latency, cost, and provenance.</p></article>
            <article><h3>Review</h3><p>Keep protected truth outside the strategy and public viewer.</p></article>
            <article><h3>Decide</h3><p>Require Kevin's exact-hash approval after bounded agent review.</p></article>
          </div>
        </section>

        <Architecture />

        <section aria-labelledby="findings-title" className="story-section story-findings" id="findings">
          <div className="story-section__heading">
            <h2 id="findings-title">The baseline survived both promotion gates.</h2>
            <p>I found that component improvements were not enough. A technique had to clear the full frozen decision rule.</p>
          </div>
          <div className="story-findings__approved">
            <article>
              <header><BoundGateLabel id="g2.gate" /><span>Approved retrieval result</span></header>
              <h3>I retained <BoundValue id="g2.retained_retriever" />.</h3>
              <p>No retriever was promoted. Failed ancestors and missing target-family evidence blocked the incremental candidate.</p>
              <div className="story-findings__metrics">
                <p><strong><BoundValue id="g2.generation_cells" /></strong><span>completed generation cells</span></p>
                <p><strong><BoundValue id="g2.repeat_cells" /></strong><span>repeat-evidence cells</span></p>
              </div>
              <EvidenceBinding ids={['g2.decision', 'g2.retained_retriever', 'g2.promoted_retriever', 'g2.generation_cells', 'g2.repeat_cells']} />
            </article>
            <article>
              <header><BoundGateLabel id="g3.gate" /><span>Approved temporal-memory result</span></header>
              <h3>I promoted no memory policy.</h3>
              <p>The tested memory policies failed, regressed, or remained descriptive under the preregistered rules.</p>
              <div className="story-findings__metrics">
                <p><strong><BoundValue id="g3.receipt_count" /></strong><span>public receipt commitments</span></p>
              </div>
              <EvidenceBinding ids={['g3.decision', 'g3.promoted_memory_policy', 'g3.receipt_count', 'g3.human_reviewer_role']} />
            </article>
          </div>
          <div className="story-findings__frontier">
            <article>
              <header><BoundValue id="f3.experiment" /><span>Virtual context paging</span></header>
              <p><BoundValue id="f3.final_status" />. Useful as a negative demonstration. It authorizes no promotion claim.</p>
              <EvidenceBinding ids={['f3.experiment', 'f3.final_status', 'f3.human_status']} />
            </article>
            <article>
              <header><BoundValue id="f5.experiment" /><span>Bounded search</span></header>
              <p><BoundValue id="f5.final_status" />. Useful as a negative demonstration. It authorizes no general superiority claim.</p>
              <EvidenceBinding ids={['f5.experiment', 'f5.final_status', 'f5.human_status']} />
            </article>
          </div>
        </section>

        <section aria-labelledby="negative-title" className="story-section story-negative">
          <p className="story-eyebrow">What I chose not to ship</p>
          <h2 id="negative-title">More machinery. No promotion.</h2>
          <div className="story-negative__ledger">
            <p><span>Retrieval</span>More stages did not clear the full gate.</p>
            <p><span>Memory</span>The tested policies did not clear the frozen temporal gate.</p>
            <p><span>Frontier</span>More search remained a bounded negative demonstration.</p>
          </div>
          <p className="story-negative__conclusion">In ContextLab, complexity is rejected until the evidence pays for it.</p>
        </section>

        <TemporalCase />
        <TraceExplorer />

        <section aria-labelledby="role-title" className="story-section story-role" id="role">
          <div className="story-section__heading">
            <h2 id="role-title">I owned the research and every final decision.</h2>
            <p>Agents extended my execution capacity. They did not own the question, the claims, or the approval boundary.</p>
          </div>
          <dl className="story-role__map">
            <div><dt>Research lead</dt><dd>I conceived the project and defined the research questions.</dd></div>
            <div><dt>Systems architect</dt><dd>I designed the adapters, evidence boundary, receipts, and gates.</dd></div>
            <div><dt>Product owner</dt><dd>I set scope, priorities, acceptance rules, and public claim limits.</dd></div>
            <div><dt>Agent orchestrator</dt><dd>I assigned bounded work and reviewed the returned evidence.</dd></div>
            <div><dt>Final human authority</dt><dd>I audited the record and made every human decision.</dd></div>
          </dl>
          <div className="story-role__governance">
            <article>
              <h3>Kevin could approve.</h3>
              <p>Kevin is the sole human reviewer. He bound each decision to the current artifact hash.</p>
            </article>
            <article>
              <h3>Agents could not approve.</h3>
              <p>Agents performed bounded implementation and review work. No agent could approve its own output.</p>
            </article>
          </div>
        </section>

        <section aria-labelledby="limits-title" className="story-section story-limits" id="limits">
          <div className="story-section__heading">
            <h2 id="limits-title">The claim boundary is part of the result.</h2>
            <p>ContextLab is a postgraduate research platform with frozen, reproducible evidence. Its claims stay narrow.</p>
          </div>
          <ul>
            <li>The conclusions apply to the frozen synthetic NovaLearn benchmark.</li>
            <li>The work is not peer-reviewed, publication-grade, or production-proven.</li>
            <li>The memory result does not show that memory is universally harmful.</li>
            <li>Kevin is the sole human reviewer. Agent review is separate and bounded.</li>
            <li>The frontier results are demonstrations. They do not authorize promotion or superiority claims.</li>
          </ul>
          <aside lang="pt-BR" className="story-portuguese">
            <h3>Contexto acadêmico</h3>
            <p>
              Este projeto nasceu como um TCC de pós-graduação da PUCRS. Kevin Araujo definiu a pesquisa, construiu o sistema de avaliação, dirigiu o trabalho dos agentes e tomou todas as decisões humanas finais.
            </p>
          </aside>
        </section>

        <section aria-labelledby="sources-title" className="story-section story-sources">
          <div>
            <h2 id="sources-title">Inspect the work at the depth you need.</h2>
            <p>The Story gives the decision path. The lab keeps the saved runs and provenance available.</p>
          </div>
          <nav aria-label="Case study sources">
            <a href="#methods" onClick={() => onOpenLab('methods')}>Method and sources</a>
            <a href="#replay" onClick={() => onOpenLab('replay')}>Saved run replay</a>
            <SourceLink evidenceId="g2.gate">Approved retrieval gate</SourceLink>
            <SourceLink evidenceId="g3.gate">Approved memory gate</SourceLink>
            {storyLinks.map((link) => (
              <a href={link.href} key={link.id}>{link.label}</a>
            ))}
          </nav>
          <p className="story-sources__release">Source publication is bound to the exact release packet and Kevin's final approval.</p>
        </section>
      </main>
      <footer className="story-footer">
        <span>ContextLab</span>
        <span>Research and final authority: Kevin Araujo</span>
      </footer>
    </div>
  );
}
