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
    detail: 'Versioned enterprise knowledge enters through a frozen public snapshot.',
  },
  {
    name: 'Strategy adapters',
    detail: 'Retrieval and memory policies run behind one comparable contract.',
  },
  {
    name: 'Context packs',
    detail: 'Selected evidence is ordered, budgeted, and bound to source IDs.',
  },
  {
    name: 'Provider gateway',
    detail: 'Every model call records route, cost, latency, and raw output.',
  },
  {
    name: 'Grading and gates',
    detail: 'Frozen rules decide whether added machinery earns promotion.',
  },
  {
    name: 'Evidence viewer',
    detail: 'Public traces expose runs, citations, commitments, and decisions.',
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
        <BoundValue id="trace.current_candidate" /> from the public event stream. The conflict stayed
        visible for context construction.
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
    title: 'Independent review checked the complete packet.',
    body: (
      <p>
        The frozen AI review gate reached <BoundValue id="g3.review_status" />. Its findings were saved
        separately from the final human decision.
      </p>
    ),
    evidenceIds: ['g3.review_status'],
  },
  {
    id: 'decision',
    label: 'Gate decision',
    title: 'The evidence did not justify added memory.',
    body: (
      <p>
        Kevin designed the gate and made the final <BoundValue id="g3.decision" /> decision. No memory
        policy was promoted.
      </p>
    ),
    evidenceIds: ['g3.decision', 'g3.human_reviewer_role', 'g3.promoted_memory_policy'],
  },
] as const;

const buildDisciplines = [
  {
    label: 'Research design',
    title: 'A falsifiable question, not a feature demo.',
    detail: 'Frozen hypotheses, controls, budgets, stop rules, and claim limits.',
  },
  {
    label: 'Synthetic data',
    title: 'An enterprise that can change over time.',
    detail: 'Policies, records, conflicts, authority levels, and supersession events.',
  },
  {
    label: 'Evaluation engine',
    title: 'Strategies compared behind one contract.',
    detail: 'Retrieval, memory, generation, grading, cost, and latency stay observable.',
  },
  {
    label: 'Evidence interface',
    title: 'Every claim can be inspected.',
    detail: 'Local exports, exact JSON pointers, source identity, and SHA-256 commitments.',
  },
] as const;

const ownershipScope = [
  ['Conceived', 'Research question, thesis, scope, and benchmark'],
  ['Designed', 'Architecture, experimental controls, gates, and product'],
  ['Built', 'Corpus, evaluation engine, evidence pipeline, and interface'],
  ['Executed', 'Experiments, review workflow, failure analysis, and verification'],
  ['Authored', 'TCC, case study, methodology, claims, and presentation'],
  ['Decided', 'Promotion criteria, no-ship calls, limitations, and release'],
] as const;

function StoryHeader({ onOpenLab }: StoryProps) {
  return (
    <header className="story-header">
      <div className="story-header__inner">
        <a className="story-brand" href="#story" aria-label="ContextLab Story home">
          <span aria-hidden className="story-brand__mark">C</span>
          <span className="story-brand__wordmark">ContextLab</span>
          <span className="story-brand__author">by Kevin Araujo</span>
        </a>
        <nav aria-label="Story sections" className="story-nav">
          <a href="#question">Question</a>
          <a href="#system">System</a>
          <a href="#findings">Findings</a>
          <a href="#role">Authorship</a>
        </nav>
        <a
          className="story-cta story-cta--compact"
          href="#comparison"
          onClick={() => onOpenLab('comparison')}
        >
          <span>Open the evidence lab</span>
          <span aria-hidden className="story-cta__icon"><ArrowRight size={16} /></span>
        </a>
      </div>
    </header>
  );
}

function EvidenceInstrument() {
  return (
    <div aria-label="ContextLab evidence architecture" className="story-instrument">
      <div className="story-instrument__header">
        <span>Evidence path</span>
        <span>Frozen and replayable</span>
      </div>
      <div className="story-instrument__field">
        <span className="story-instrument__node story-instrument__node--corpus">Corpus</span>
        <span className="story-instrument__node story-instrument__node--events">Events</span>
        <span className="story-instrument__node story-instrument__node--authority">Authority</span>
        <span className="story-instrument__node story-instrument__node--time">Time</span>
        <div className="story-instrument__core">
          <span>Selected evidence</span>
          <strong>Context pack</strong>
          <small>ordered, budgeted, source-bound</small>
        </div>
        <span className="story-instrument__node story-instrument__node--answer">Answer</span>
        <span className="story-instrument__node story-instrument__node--citations">Citations</span>
        <span className="story-instrument__node story-instrument__node--gate">Gate</span>
      </div>
      <div className="story-instrument__readout">
        <p><strong><BoundValue id="g2.generation_cells" /></strong><span>generation cells</span></p>
        <p><strong><BoundValue id="g3.receipt_count" /></strong><span>public commitments</span></p>
        <p><strong><BoundValue id="g2.retained_retriever" /></strong><span>retriever retained</span></p>
      </div>
    </div>
  );
}

function Architecture() {
  return (
    <section aria-labelledby="architecture-title" className="story-section story-architecture" id="architecture">
      <div className="story-section__heading">
        <h2 id="architecture-title">Truth stays outside the system under test.</h2>
        <p>I designed the boundary so a strategy can change without seeing the answers used to grade it.</p>
      </div>
      <div className="story-architecture__frame">
        <div className="story-architecture__public">
          <p className="story-architecture__boundary-label">Public experiment boundary</p>
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
          <p>Protected truth returns content-free metrics and commitments, never gold answers.</p>
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
        <p>Move the control to see a later, higher-authority event replace the active claim without erasing history.</p>
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
        <h2 id="trace-title">One answer. Every decision behind it.</h2>
        <p>Replay a real saved run from candidate retrieval through the final gate.</p>
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
            <p className="story-eyebrow">Postgraduate AI research and engineering</p>
            <h1 id="story-hero-title">I built ContextLab to test AI complexity.</h1>
            <p>A governed platform for testing retrieval, temporal memory, and bounded search against changing enterprise knowledge.</p>
            <div className="story-hero__actions">
              <a className="story-cta" href="#question">
                <span>See what I built</span>
                <span aria-hidden className="story-cta__icon"><ArrowRight size={16} /></span>
              </a>
              <a className="story-text-link" href="#comparison" onClick={() => onOpenLab('comparison')}>
                Inspect the evidence
              </a>
            </div>
          </div>
          <EvidenceInstrument />
        </section>

        <section aria-label="Project evidence at a glance" className="story-proofline">
          <p><strong><BoundValue id="g2.generation_cells" /></strong><span>completed generation cells</span></p>
          <p><strong><BoundValue id="g2.repeat_cells" /></strong><span>repeat-evidence cells</span></p>
          <p><strong><BoundValue id="g3.receipt_count" /></strong><span>public receipt commitments</span></p>
          <p><strong>2</strong><span>approved no-promotion gates</span></p>
        </section>

        <section aria-labelledby="problem-title" className="story-section story-problem" id="problem">
          <div className="story-section__heading">
            <h2 id="problem-title">Enterprise knowledge changes. Most AI systems pretend it does not.</h2>
            <p>A plausible answer can still be stale, conflicted, or sourced from the wrong moment.</p>
          </div>
          <div className="story-problem__states">
            <article><h3>Stale</h3><p>An older rule remains easy to retrieve after a newer rule takes effect.</p></article>
            <article><h3>Conflicting</h3><p>Two sources make incompatible claims with different authority.</p></article>
            <article><h3>Changing</h3><p>The correct answer changes with event order and the requested snapshot.</p></article>
          </div>
        </section>

        <section aria-labelledby="question-title" className="story-section story-question" id="question">
          <div>
            <h2 id="question-title">The question behind the whole system</h2>
            <blockquote>
              When should enterprise AI retrieve more, remember more, search more, and when should it stay simple?
            </blockquote>
          </div>
          <aside>
            <span>NovaLearn</span>
            <h3>A synthetic enterprise built for hard cases.</h3>
            <p>I created policies, customer records, conflicts, authority levels, and temporal events so the experiment can be repeated safely.</p>
          </aside>
        </section>

        <section aria-labelledby="system-title" className="story-section story-system" id="system">
          <div className="story-section__heading">
            <h2 id="system-title">A thesis turned into a working research platform.</h2>
            <p>I built the data, experiment engine, governance layer, and public evidence interface as one system.</p>
          </div>
          <div className="story-system__disciplines">
            {buildDisciplines.map((discipline) => (
              <article key={discipline.label}>
                <span>{discipline.label}</span>
                <h3>{discipline.title}</h3>
                <p>{discipline.detail}</p>
              </article>
            ))}
          </div>
          <p className="story-system__stack">
            <span>Python</span><span>React 19</span><span>TypeScript</span><span>Vite</span>
            <span>JSON Schema</span><span>SHA-256 provenance</span><span>Provider gateways</span>
          </p>
        </section>

        <section aria-labelledby="controls-title" className="story-section story-controls">
          <div className="story-section__heading">
            <h2 id="controls-title">The comparison stays honest by construction.</h2>
            <p>The strategy changes. The tasks, model route, budgets, gate rules, and evaluator boundary do not.</p>
          </div>
          <div className="story-controls__sequence">
            <article><h3>Freeze the study</h3><p>Bind corpus, tasks, configuration, budgets, and promotion criteria.</p></article>
            <article><h3>Instrument every run</h3><p>Save context, output, citations, latency, cost, and provenance.</p></article>
            <article><h3>Keep truth sealed</h3><p>Protect gold answers from retrieval, generation, memory, and the viewer.</p></article>
            <article><h3>Reject what fails</h3><p>Preserve the evidence and keep extra machinery out of the architecture.</p></article>
          </div>
        </section>

        <Architecture />

        <section aria-labelledby="findings-title" className="story-section story-findings" id="findings">
          <div className="story-section__heading">
            <h2 id="findings-title">The strongest result was knowing what not to ship.</h2>
            <p>Component wins were not enough. Every technique had to clear the complete frozen decision rule.</p>
          </div>
          <div className="story-findings__approved">
            <article>
              <header><BoundGateLabel id="g2.gate" /><span>Retrieval</span></header>
              <h3>Keep <BoundValue id="g2.retained_retriever" />.</h3>
              <p>No advanced retriever earned promotion across the complete approved gate.</p>
              <div className="story-findings__metrics">
                <p><strong><BoundValue id="g2.generation_cells" /></strong><span>generation cells</span></p>
                <p><strong><BoundValue id="g2.repeat_cells" /></strong><span>repeat cells</span></p>
              </div>
              <EvidenceBinding ids={['g2.decision', 'g2.retained_retriever', 'g2.promoted_retriever', 'g2.generation_cells', 'g2.repeat_cells']} />
            </article>
            <article>
              <header><BoundGateLabel id="g3.gate" /><span>Temporal memory</span></header>
              <h3>Promote nothing.</h3>
              <p>The tested memory policies failed, regressed, or stayed descriptive under the preregistered rules.</p>
              <div className="story-findings__metrics">
                <p><strong><BoundValue id="g3.receipt_count" /></strong><span>public commitments</span></p>
              </div>
              <EvidenceBinding ids={['g3.decision', 'g3.promoted_memory_policy', 'g3.receipt_count', 'g3.human_reviewer_role']} />
            </article>
          </div>
          <div className="story-findings__frontier">
            <article><header><BoundValue id="f3.experiment" /><span>Virtual context paging</span></header><p><BoundValue id="f3.final_status" />. A useful negative demonstration, not a promotion claim.</p><EvidenceBinding ids={['f3.experiment', 'f3.final_status', 'f3.human_status']} /></article>
            <article><header><BoundValue id="f5.experiment" /><span>Bounded search</span></header><p><BoundValue id="f5.final_status" />. More search did not meet the frozen success rule.</p><EvidenceBinding ids={['f5.experiment', 'f5.final_status', 'f5.human_status']} /></article>
          </div>
        </section>

        <section aria-labelledby="negative-title" className="story-section story-negative">
          <p className="story-eyebrow">The product judgment</p>
          <h2 id="negative-title">More machinery did not earn a place in the system.</h2>
          <div className="story-negative__ledger">
            <p><span>Retrieval</span>More stages did not clear the full gate.</p>
            <p><span>Memory</span>The tested policies did not clear the temporal gate.</p>
            <p><span>Search</span>More exploration stayed a bounded negative demonstration.</p>
          </div>
          <p className="story-negative__conclusion">Negative results are not failure here. They are evidence that the governance works.</p>
        </section>

        <TemporalCase />
        <TraceExplorer />

        <section aria-labelledby="role-title" className="story-section story-role" id="role">
          <div className="story-role__statement">
            <p className="story-eyebrow">Built end to end by</p>
            <h2 id="role-title">Kevin Araujo</h2>
            <p>I conceived, designed, built, ran, analyzed, documented, and presented ContextLab. It is my complete research and engineering project.</p>
          </div>
          <dl className="story-role__map">
            {ownershipScope.map(([term, definition]) => (
              <div key={term}><dt>{term}</dt><dd>{definition}</dd></div>
            ))}
          </dl>
          <p className="story-role__clarification">
            I used AI systems as tools inside the workflow, just as I used Python and React. The authorship, implementation, research judgment, and project ownership are mine.
          </p>
        </section>

        <section aria-labelledby="limits-title" className="story-section story-limits" id="limits">
          <div className="story-section__heading">
            <h2 id="limits-title">The limits are part of the result.</h2>
            <p>ContextLab makes narrow, reproducible claims instead of pretending one benchmark proves everything.</p>
          </div>
          <ul>
            <li>The conclusions apply to the frozen synthetic NovaLearn benchmark.</li>
            <li>The work is postgraduate research, not a peer-reviewed production study.</li>
            <li>The memory result does not show that memory is universally harmful.</li>
            <li>Kevin is the sole human reviewer, a limitation stated throughout the work.</li>
            <li>The frontier results are demonstrations, not superiority claims.</li>
          </ul>
          <aside lang="pt-BR" className="story-portuguese">
            <h3>Contexto acadêmico</h3>
            <p>O ContextLab nasceu como meu TCC de pós-graduação da PUCRS. Eu concebi, projetei, construí, executei, analisei e documentei o projeto completo.</p>
          </aside>
        </section>

        <section aria-labelledby="sources-title" className="story-section story-sources">
          <div>
            <h2 id="sources-title">Inspect it at the depth you need.</h2>
            <p>Start with the guided story, then open the saved runs, methods, source code, and exact evidence.</p>
          </div>
          <nav aria-label="Case study sources">
            <a href="#methods" onClick={() => onOpenLab('methods')}>Method and sources</a>
            <a href="#replay" onClick={() => onOpenLab('replay')}>Saved run replay</a>
            <SourceLink evidenceId="g2.gate">Approved retrieval gate</SourceLink>
            <SourceLink evidenceId="g3.gate">Approved memory gate</SourceLink>
            {storyLinks.map((link) => <a href={link.href} key={link.id}>{link.label}</a>)}
          </nav>
          <p className="story-sources__release">Every public result is bound to its source artifact and exact release record.</p>
        </section>
      </main>
      <footer className="story-footer">
        <span>ContextLab</span>
        <span>Conceived, designed, built, and authored by Kevin Araujo</span>
      </footer>
    </div>
  );
}
