import { useState, type KeyboardEvent } from 'react';
import ArrowRight from '@carbon/icons-react/es/ArrowRight';

import { BoundGateLabel, BoundNumber, BoundValue, EvidenceBinding } from './EvidenceBinding';
import { formatEvidenceValue, getEvidence, publicArtifactHref } from './evidence';
import { storyLinks } from './links';
import { useActiveSection } from './motion';

export type StoryLabView = 'comparison' | 'methods' | 'replay';

interface StoryProps {
  onOpenLab: (view: StoryLabView) => void;
}

const SECTIONS = [
  { id: 'result', index: '01', label: 'Result' },
  { id: 'problem', index: '02', label: 'Problem' },
  { id: 'system', index: '03', label: 'System' },
  { id: 'trace', index: '04', label: 'Trace' },
  { id: 'author', index: '05', label: 'Author' },
  { id: 'limits', index: '06', label: 'Limits' },
] as const;

const SECTION_IDS = SECTIONS.map((section) => section.id);

/** The four frozen studies, summarised exactly as the approved gates recorded them. */
const ledger = [
  {
    id: 'g2',
    code: 'G2',
    subject: 'Retrieval',
    scale: { kind: 'bound', id: 'g2.generation_cells', unit: 'generation cells' },
    verdict: 'Retain R0',
    note: 'No advanced retriever promoted',
  },
  {
    id: 'g3',
    code: 'G3',
    subject: 'Temporal memory',
    scale: { kind: 'bound', id: 'g3.receipt_count', unit: 'public receipts' },
    verdict: 'Promote nothing',
    note: 'No memory policy promoted',
  },
  {
    id: 'f3',
    code: 'F3',
    subject: 'Virtual context paging',
    scale: { kind: 'text', text: '—', unit: 'demonstration' },
    verdict: 'Accepted-negative',
    note: 'Not a promotion or significance claim',
  },
  {
    id: 'f5',
    code: 'F5',
    subject: 'Bounded search',
    scale: { kind: 'text', text: '—', unit: 'demonstration' },
    verdict: 'Accepted-negative',
    note: 'More search did not meet the success rule',
  },
] as const;

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

const experimentControls = [
  { step: 'Freeze', title: 'Bind the study before it runs.', detail: 'Corpus, tasks, configuration, budgets, and promotion criteria are fixed first.' },
  { step: 'Instrument', title: 'Record everything a run touched.', detail: 'Context, output, citations, latency, cost, and provenance are saved per cell.' },
  { step: 'Seal', title: 'Keep the answers out of reach.', detail: 'Gold answers stay outside retrieval, generation, memory, and the viewer.' },
  { step: 'Reject', title: 'Let the rule decide, not the demo.', detail: 'Machinery that misses the frozen rule stays out, and the evidence is kept.' },
] as const;

const ownershipScope = [
  ['Conceived', 'Research question, thesis, scope, and benchmark'],
  ['Designed', 'Architecture, experimental controls, gates, and product'],
  ['Built', 'Corpus, evaluation engine, evidence pipeline, and interface'],
  ['Executed', 'Experiments, review workflow, failure analysis, and verification'],
  ['Authored', 'TCC, case study, methodology, claims, and presentation'],
  ['Decided', 'Promotion criteria, no-ship calls, limitations, and release'],
] as const;

const limits = [
  'The conclusions apply to the frozen synthetic NovaLearn benchmark.',
  'The work is postgraduate research, not a peer-reviewed production study.',
  'The memory result does not show that memory is universally harmful.',
  'Kevin is the sole human reviewer, a limitation stated throughout the work.',
  'The frontier results are demonstrations, not superiority claims.',
] as const;

function StoryHeader({ onOpenLab, active }: StoryProps & { active: string }) {
  return (
    <header className="sh">
      <div className="sh__inner">
        <a className="sh__brand" href="#top" aria-label="ContextLab, home">
          <span aria-hidden className="sh__mark">
            <span />
          </span>
          <span className="sh__word">ContextLab</span>
          <span className="sh__by">Kevin Araujo</span>
        </a>
        <nav aria-label="Story sections" className="sh__nav">
          {SECTIONS.slice(0, 5).map((section) => (
            <a
              aria-current={active === section.id ? 'true' : undefined}
              href={`#${section.id}`}
              key={section.id}
            >
              <span className="sh__nav-index">{section.index}</span>
              {section.label}
            </a>
          ))}
        </nav>
        <a className="cta cta--sm" href="#comparison" onClick={() => onOpenLab('comparison')}>
          <span>Open the evidence lab</span>
          <span aria-hidden className="cta__icon"><ArrowRight size={16} /></span>
        </a>
      </div>
      <div aria-hidden className="sh__progress"><i /></div>
    </header>
  );
}

function SectionRail({ active }: { active: string }) {
  return (
    <nav aria-label="Section progress" className="rail">
      <ol>
        {SECTIONS.map((section) => (
          <li key={section.id}>
            <a aria-current={active === section.id ? 'true' : undefined} href={`#${section.id}`}>
              <span className="rail__index">{section.index}</span>
              <span className="rail__tick" aria-hidden />
              <span className="rail__label">{section.label}</span>
            </a>
          </li>
        ))}
      </ol>
    </nav>
  );
}

/** The whole project, readable in four lines, before a visitor scrolls once. */
function ResultLedger() {
  return (
    <figure className="ledger">
      <figcaption className="ledger__head">
        <span>Frozen result ledger</span>
        <span className="ledger__release">portfolio-v1</span>
      </figcaption>
      <div className="ledger__rows">
        {ledger.map((row) => (
          <div className="ledger__row" key={row.id}>
            <span className="ledger__code">{row.code}</span>
            <span className="ledger__subject">{row.subject}</span>
            <span className="ledger__scale">
              {row.scale.kind === 'bound' ? (
                <BoundNumber id={row.scale.id} />
              ) : (
                <em>{row.scale.text}</em>
              )}
              <small>{row.scale.unit}</small>
            </span>
            <span className="ledger__verdict">
              <span className="ledger__arrow" aria-hidden>→</span>
              {row.verdict}
            </span>
            <span className="ledger__note">{row.note}</span>
          </div>
        ))}
      </div>
      <div className="ledger__foot">
        <span><strong>0</strong> techniques promoted</span>
        <span><strong>4</strong> studies completed</span>
        <span><strong>0</strong> results withdrawn</span>
      </div>
    </figure>
  );
}

function Hero({ onOpenLab }: StoryProps) {
  return (
    <section aria-labelledby="story-hero-title" className="hero" id="top">
      <div className="hero__copy">
        <p className="eyebrow">Postgraduate AI research and engineering</p>
        <h1 id="story-hero-title">
          I built a laboratory to find out when AI complexity earns its place.
        </h1>
        <p className="hero__lede">
          Two preregistered gates and two frontier experiments, run against a synthetic enterprise
          whose knowledge changes underneath the question. Nothing earned promotion. Every figure on
          this page is bound to the artifact it came from.
        </p>
        <div className="hero__actions">
          <a className="cta" href="#result">
            <span>Read the result</span>
            <span aria-hidden className="cta__icon"><ArrowRight size={16} /></span>
          </a>
          <a className="linky" href="#comparison" onClick={() => onOpenLab('comparison')}>
            Inspect the saved runs
          </a>
        </div>
        <p className="hero__legend">
          <span aria-hidden className="hero__swatch" />
          Values in amber are read from a frozen artifact at an exact JSON pointer. Open any binding
          to see the file, the pointer, and its SHA-256.
        </p>
      </div>
      <ResultLedger />
    </section>
  );
}

function Findings() {
  return (
    <section aria-labelledby="result-title" className="sec sec--result" id="result">
      <div className="sec__head">
        <p className="eyebrow"><span className="sec__num">01</span> The result</p>
        <h2 id="result-title">The strongest result was knowing what not to ship.</h2>
        <p className="sec__lede">
          Component wins were not enough. Every technique had to clear the complete decision rule
          that was frozen before the campaign started.
        </p>
      </div>

      <div className="gates">
        <article className="gate">
          <header>
            <span className="gate__code"><BoundGateLabel id="g2.gate" /></span>
            <span className="gate__subject">Retrieval</span>
          </header>
          <h3>Keep <BoundValue id="g2.retained_retriever" />, the simplest retriever in the study.</h3>
          <p>No advanced retriever earned promotion across the complete approved gate.</p>
          <dl className="gate__metrics">
            <div>
              <dt>Generation cells</dt>
              <dd><BoundNumber id="g2.generation_cells" /></dd>
            </div>
            <div>
              <dt>Repeat-evidence cells</dt>
              <dd><BoundNumber id="g2.repeat_cells" /></dd>
            </div>
            <div>
              <dt>Promoted</dt>
              <dd className="gate__none">none</dd>
            </div>
          </dl>
          <EvidenceBinding
            ids={['g2.decision', 'g2.retained_retriever', 'g2.promoted_retriever', 'g2.generation_cells', 'g2.repeat_cells']}
          />
        </article>

        <article className="gate">
          <header>
            <span className="gate__code"><BoundGateLabel id="g3.gate" /></span>
            <span className="gate__subject">Temporal memory</span>
          </header>
          <h3>Promote nothing, and say so in the record.</h3>
          <p>The tested memory policies failed, regressed, or stayed descriptive under the preregistered rules.</p>
          <dl className="gate__metrics">
            <div>
              <dt>Public receipts</dt>
              <dd><BoundNumber id="g3.receipt_count" /></dd>
            </div>
            <div>
              <dt>Review status</dt>
              <dd><BoundValue id="g3.review_status" /></dd>
            </div>
            <div>
              <dt>Promoted</dt>
              <dd className="gate__none">none</dd>
            </div>
          </dl>
          <EvidenceBinding
            ids={['g3.decision', 'g3.promoted_memory_policy', 'g3.receipt_count', 'g3.human_reviewer_role']}
          />
        </article>
      </div>

      <div className="frontier">
        <p className="frontier__label">Frontier demonstrations</p>
        <article>
          <h3><BoundValue id="f3.experiment" /> · Virtual context paging</h3>
          <p><BoundValue id="f3.final_status" />. A useful negative demonstration, not a promotion claim.</p>
          <EvidenceBinding ids={['f3.experiment', 'f3.final_status', 'f3.human_status']} />
        </article>
        <article>
          <h3><BoundValue id="f5.experiment" /> · Bounded search</h3>
          <p><BoundValue id="f5.final_status" />. More search did not meet the frozen success rule.</p>
          <EvidenceBinding ids={['f5.experiment', 'f5.final_status', 'f5.human_status']} />
        </article>
      </div>

      <blockquote className="pull">
        <p>
          Negative results are not failure here. They are evidence that the governance works.
        </p>
        <cite>The product judgment behind ContextLab</cite>
      </blockquote>
    </section>
  );
}

/** Authority axis for the temporal demonstration, drawn from the bound event values. */
function TemporalPlot({ isCurrent }: { isCurrent: boolean }) {
  const earlierAuthority = Number(getEvidence('temporal.earlier_authority').value);
  const currentAuthority = Number(getEvidence('temporal.current_authority').value);
  const max = 6;
  const y = (authority: number): number => 148 - (authority / max) * 112;

  return (
    <svg
      aria-hidden
      className="plot"
      viewBox="0 0 420 176"
      xmlns="http://www.w3.org/2000/svg"
    >
      {[1, 2, 3, 4, 5, 6].map((level) => (
        <line className="plot__grid" key={level} x1="34" x2="410" y1={y(level)} y2={y(level)} />
      ))}
      <line className="plot__axis" x1="34" x2="34" y1="24" y2="148" />
      <line className="plot__axis" x1="34" x2="410" y1="148" y2="148" />
      <text className="plot__axis-label" x="34" y="168">earlier</text>
      <text className="plot__axis-label" textAnchor="end" x="410" y="168">later</text>
      <text className="plot__axis-label" transform="rotate(-90 14 96)" x="14" y="96">authority</text>

      <line
        className={isCurrent ? 'plot__link plot__link--on' : 'plot__link'}
        x1="140"
        x2="318"
        y1={y(earlierAuthority)}
        y2={y(currentAuthority)}
      />
      <g className={isCurrent ? 'plot__node plot__node--past' : 'plot__node plot__node--on'}>
        <circle cx="140" cy={y(earlierAuthority)} r="7" />
        <text x="140" y={y(earlierAuthority) - 16} textAnchor="middle">TL-07-E01</text>
      </g>
      <g className={isCurrent ? 'plot__node plot__node--on' : 'plot__node plot__node--future'}>
        <circle cx="318" cy={y(currentAuthority)} r="7" />
        <text x="318" y={y(currentAuthority) - 16} textAnchor="middle">TL-07-E02</text>
      </g>
      <line className="plot__head" x1={isCurrent ? 372 : 196} x2={isCurrent ? 372 : 196} y1="24" y2="148" />
    </svg>
  );
}

function Problem() {
  const [moment, setMoment] = useState(0);
  const isCurrent = moment === 1;
  const earlierState = isCurrent ? getEvidence('temporal.earlier_state') : null;
  const currentState = getEvidence('temporal.current_state');

  return (
    <section aria-labelledby="problem-title" className="sec sec--problem" id="problem">
      <div className="sec__head">
        <p className="eyebrow"><span className="sec__num">02</span> The problem</p>
        <h2 id="problem-title">A correct answer depends on when you ask.</h2>
        <p className="sec__lede">
          Enterprise knowledge goes stale, conflicts with itself, and gets superseded by sources with
          more authority. A plausible answer can still be sourced from the wrong moment.
        </p>
      </div>

      <div className="temporal">
        <div className="temporal__viz">
          <TemporalPlot isCurrent={isCurrent} />
          <div className="temporal__control">
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
            <div aria-hidden className="temporal__ticks">
              <span>Earlier evidence</span>
              <span>Later evidence</span>
            </div>
          </div>
        </div>

        <div aria-live="polite" className="temporal__read" data-moment={isCurrent ? 'current' : 'earlier'}>
          <p className="temporal__question">
            &ldquo;At this snapshot, what is the approved primary audience for the sales ICP?&rdquo;
          </p>
          <article className={isCurrent ? 'claim claim--superseded' : 'claim claim--active'}>
            <span className="claim__state">{earlierState ? formatEvidenceValue(earlierState) : 'active at this point'}</span>
            <h3><BoundValue id="temporal.earlier_value" /></h3>
            <p className="claim__meta">
              <span>TL-07-E01</span>
              <span>authority <BoundValue id="temporal.earlier_authority" /></span>
            </p>
          </article>
          {isCurrent ? (
            <article className="claim claim--active">
              <span className="claim__state">{formatEvidenceValue(currentState)}</span>
              <h3><BoundValue id="temporal.current_value" /></h3>
              <p className="claim__meta">
                <span>TL-07-E02</span>
                <span>authority <BoundValue id="temporal.current_authority" /></span>
              </p>
            </article>
          ) : (
            <div className="claim claim--pending">
              Later evidence has not entered the snapshot.
            </div>
          )}
        </div>
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

function System() {
  return (
    <section aria-labelledby="system-title" className="sec sec--system" id="system">
      <div className="sec__head">
        <p className="eyebrow"><span className="sec__num">03</span> The system</p>
        <h2 id="system-title">Truth stays outside the system under test.</h2>
        <p className="sec__lede">
          I designed the boundary so a strategy can change without ever seeing the answers used to
          grade it. The strategy varies; the tasks, model route, budgets, and gate rules do not.
        </p>
      </div>

      <div className="arch">
        <div className="arch__public">
          <p className="arch__boundary">Public experiment boundary</p>
          <ol aria-label="ContextLab system architecture" className="arch__flow">
            {architectureNodes.map((node, index) => (
              <li key={node.name}>
                <span aria-hidden className="arch__step">{String(index + 1).padStart(2, '0')}</span>
                <strong>{node.name}</strong>
                <span className="arch__detail">{node.detail}</span>
              </li>
            ))}
          </ol>
        </div>
        <aside aria-label="Sealed evaluator boundary" className="arch__sealed">
          <span className="arch__sealed-tag">Outside the public boundary</span>
          <h3>Sealed evaluator</h3>
          <p>Protected truth returns content-free metrics and commitments, never gold answers.</p>
          <p className="arch__sealed-wire" aria-hidden>
            <span>grades · labels · aggregates · commitments</span>
          </p>
        </aside>
      </div>

      <div className="controls">
        {experimentControls.map((control) => (
          <article key={control.step}>
            <span className="controls__step">{control.step}</span>
            <h3>{control.title}</h3>
            <p>{control.detail}</p>
          </article>
        ))}
      </div>

      <div className="disciplines">
        <p className="disciplines__label">Built as one system, across four disciplines</p>
        <div className="disciplines__grid">
          {buildDisciplines.map((discipline) => (
            <article key={discipline.label}>
              <span>{discipline.label}</span>
              <h3>{discipline.title}</h3>
              <p>{discipline.detail}</p>
            </article>
          ))}
        </div>
        <p className="stack">
          {['Python', 'React 19', 'TypeScript', 'Vite', 'JSON Schema', 'SHA-256 provenance', 'Provider gateways'].map(
            (item) => (
              <span key={item}>{item}</span>
            ),
          )}
        </p>
      </div>
    </section>
  );
}

function Trace() {
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
    <section aria-labelledby="trace-title" className="sec sec--trace" id="trace">
      <div className="sec__head">
        <p className="eyebrow"><span className="sec__num">04</span> The trace</p>
        <h2 id="trace-title">One answer. Every decision behind it.</h2>
        <p className="sec__lede">
          Replay a real saved run, from candidate retrieval through the final gate. Nothing here is
          generated live; each stage reads from the stored receipt.
        </p>
      </div>

      <div className="specimen">
        <div className="specimen__field">
          <span>Saved question</span>
          <p><BoundValue id="trace.question" /></p>
        </div>
        <div className="specimen__field specimen__field--id">
          <span>Run</span>
          <code><BoundValue id="trace.run_id" /></code>
        </div>
      </div>

      <div className="explorer">
        <div aria-label="Trace stages" className="explorer__tabs" role="tablist">
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
                <span aria-hidden className="explorer__dot" />
                {stage.label}
              </button>
            </div>
          ))}
        </div>
        <div
          aria-labelledby={`trace-tab-${activeStage.id}`}
          className="explorer__panel"
          id={`trace-panel-${activeStage.id}`}
          key={activeStage.id}
          role="tabpanel"
        >
          <p className="explorer__stage">
            Stage {activeIndex + 1} of {traceStages.length} · {activeStage.label}
          </p>
          <h3>{activeStage.title}</h3>
          {activeStage.body}
          <EvidenceBinding ids={[...activeStage.evidenceIds]} />
        </div>
      </div>
    </section>
  );
}

function Author() {
  return (
    <section aria-labelledby="author-title" className="sec sec--author" id="author">
      <div className="sec__head">
        <p className="eyebrow"><span className="sec__num">05</span> Authorship</p>
        <h2 id="author-title">Kevin Araujo</h2>
        <p className="sec__lede">
          I conceived, designed, built, ran, analyzed, documented, and presented ContextLab. It is my
          complete research and engineering project.
        </p>
      </div>
      <dl className="scope">
        {ownershipScope.map(([term, definition]) => (
          <div key={term}>
            <dt>{term}</dt>
            <dd>{definition}</dd>
          </div>
        ))}
      </dl>
      <p className="note">
        I used AI systems as tools inside the workflow, just as I used Python and React. The
        authorship, implementation, research judgment, and project ownership are mine.
      </p>
    </section>
  );
}

function SourceLink({ evidenceId, children }: { evidenceId: string; children: string }) {
  const href = publicArtifactHref(getEvidence(evidenceId));
  if (!href) return null;
  return (
    <a href={href}>
      <span>{children}</span>
      <code>json</code>
    </a>
  );
}

function Limits({ onOpenLab }: StoryProps) {
  return (
    <section aria-labelledby="limits-title" className="sec sec--limits" id="limits">
      <div className="sec__head">
        <p className="eyebrow"><span className="sec__num">06</span> Limits and sources</p>
        <h2 id="limits-title">The limits are part of the result.</h2>
        <p className="sec__lede">
          ContextLab makes narrow, reproducible claims instead of pretending one benchmark proves
          everything.
        </p>
      </div>

      <ol className="limits">
        {limits.map((limit, index) => (
          <li key={limit}>
            <span aria-hidden>{String(index + 1).padStart(2, '0')}</span>
            {limit}
          </li>
        ))}
      </ol>

      <aside lang="pt-BR" className="ptbr">
        <h3>Contexto acadêmico</h3>
        <p>
          O ContextLab nasceu como meu TCC de pós-graduação da PUCRS. Eu concebi, projetei, construí,
          executei, analisei e documentei o projeto completo.
        </p>
      </aside>

      <div className="sources">
        <h3>Inspect it at the depth you need.</h3>
        <nav aria-label="Case study sources">
          <a href="#methods" onClick={() => onOpenLab('methods')}>
            <span>Method and sources</span>
            <code>lab</code>
          </a>
          <a href="#replay" onClick={() => onOpenLab('replay')}>
            <span>Saved run replay</span>
            <code>lab</code>
          </a>
          <SourceLink evidenceId="g2.gate">Approved retrieval gate</SourceLink>
          <SourceLink evidenceId="g3.gate">Approved memory gate</SourceLink>
          {storyLinks.map((link) => (
            <a href={link.href} key={link.id}>
              <span>{link.label}</span>
              <code>{link.sha256 ? link.sha256.slice(0, 7) : 'github'}</code>
            </a>
          ))}
        </nav>
        <p className="sources__note">
          Every public result is bound to its source artifact and exact release record.
        </p>
      </div>
    </section>
  );
}

export default function Story({ onOpenLab }: StoryProps) {
  const active = useActiveSection(SECTION_IDS);

  return (
    <div className="story-theme" id="story">
      <a className="skip-link" href="#story-main">Skip to case study</a>
      <StoryHeader active={active} onOpenLab={onOpenLab} />
      <SectionRail active={active} />
      <main id="story-main" tabIndex={-1}>
        <Hero onOpenLab={onOpenLab} />
        <Findings />
        <Problem />
        <System />
        <Trace />
        <Author />
        <Limits onOpenLab={onOpenLab} />
      </main>
      <footer className="story-footer">
        <span className="story-footer__mark">ContextLab</span>
        <span>Conceived, designed, built, and authored by Kevin Araujo</span>
        <span className="story-footer__release">portfolio-v1 · PUCRS</span>
      </footer>
    </div>
  );
}
