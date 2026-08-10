import { Search, Select, SelectItem } from '@carbon/react';
import { useMemo, useState, type ChangeEvent } from 'react';

import type { ContextLabViewerExport } from '../data/contract';
import { ArtifactLink, CitationLink, MetricLink } from '../components/ProvenanceLink';
import { EmptyState } from '../components/RuntimeStates';
import { EvidenceCallout, ViewHeader } from '../components/ViewPrimitives';

export default function QuestionComparison({ data }: { data: ContextLabViewerExport }) {
  const [query, setQuery] = useState('');
  const [questionId, setQuestionId] = useState(data.questions[0]?.id ?? '');
  const runById = useMemo(() => new Map(data.runs.map((run) => [run.id, run])), [data.runs]);
  const strategyById = useMemo(
    () => new Map(data.strategies.map((strategy) => [strategy.id, strategy])),
    [data.strategies],
  );
  const questionById = useMemo(
    () => new Map(data.questions.map((question) => [question.id, question])),
    [data.questions],
  );

  function matchesQuery(question: (typeof data.questions)[number], value: string): boolean {
    const normalized = value.trim().toLowerCase();
    return (
      normalized.length === 0 ||
      `${question.id} ${question.text} ${question.taskFamily}`.toLowerCase().includes(normalized)
    );
  }

  function updateQuery(value: string): void {
    setQuery(value);
    const nextMatches = data.questions.filter((question) => matchesQuery(question, value));
    if (nextMatches.length > 0 && !nextMatches.some((question) => question.id === questionId)) {
      setQuestionId(nextMatches[0].id);
    }
  }

  if (data.questions.length === 0) {
    return (
      <EmptyState
        detail="Generate a versioned export with questions and five comparison run IDs per question."
        title="No saved questions are present"
      />
    );
  }

  const matchingQuestions = data.questions.filter((question) => matchesQuery(question, query));
  const question =
    matchingQuestions.find((item) => item.id === questionId) ??
    matchingQuestions[0] ??
    questionById.get(questionId) ??
    data.questions[0];
  const runs = question.comparisonRunIds.flatMap((runId) => {
    const run = runById.get(runId);
    return run ? [run] : [];
  });

  return (
    <section aria-labelledby="comparison-title" className="view-stack comparison-view">
      <ViewHeader
        description="Inspect the same saved question across the five frozen strategy lanes. No new answer is generated here."
        title="Question comparison"
      />
      <div className="question-stage">
        <div className="question-controls">
          <Search
            closeButtonLabelText="Clear saved question search"
            labelText="Find a saved question"
            onChange={(event: ChangeEvent<HTMLInputElement>) => updateQuery(event.target.value)}
            placeholder="Type a question, task ID, or family"
            size="lg"
            value={query}
          />
          <Select
            id="saved-question"
            labelText="Saved question"
            onChange={(event: ChangeEvent<HTMLSelectElement>) => setQuestionId(event.target.value)}
            size="lg"
            value={question.id}
          >
            {matchingQuestions.map((item) => (
              <SelectItem key={item.id} text={`${item.id} | ${item.text}`} value={item.id} />
            ))}
          </Select>
        </div>
        {data.showcase.executionFailure ? (
          <EvidenceCallout insight={data.showcase.executionFailure} />
        ) : null}
        {matchingQuestions.length === 0 ? (
          <EmptyState
            detail="The viewer can only replay questions included in the loaded export. Clear the search or generate a new export."
            title="No saved replay matches this question"
          />
        ) : (
          <>
            <div className="question-summary">
              <div className="question-summary__body">
                <span className="chip">{question.taskFamily}</span>
                <h2 id="comparison-title">{question.text}</h2>
              </div>
              <div className="question-summary__meta">
                <ArtifactLink artifact={question.artifact} compact />
              </div>
            </div>
            <div aria-label="Five strategy results" className="strategy-lanes">
              {runs.map((run, index) => {
                const strategy = strategyById.get(run.strategyId);
                const failed = run.executionStatus === 'failed';
                return (
                  <article
                    className={`strategy-lane${failed ? ' strategy-lane--failed' : ''}`}
                    data-lane-index={index}
                    key={run.id}
                  >
                    <header className="strategy-lane__header">
                      <p className="strategy-lane__title">{strategy?.label ?? run.strategyId}</p>
                      <span className="strategy-lane__summary">{strategy?.summary}</span>
                    </header>
                    <div className="strategy-lane__status">
                      <span className="chip" data-status={run.executionStatus}>
                        {run.executionStatus}
                      </span>
                      <span className="strategy-lane__id">
                        Run <strong>{run.id}</strong>
                      </span>
                      <span className="strategy-lane__id">
                        Config <strong>{run.configuration.id}</strong>
                      </span>
                    </div>
                    <div className="strategy-lane__metrics">
                      <MetricLink variant="compact" label="Context" metric={run.metrics.contextTokens} />
                      <MetricLink variant="compact" label="Latency" metric={run.metrics.latency} />
                      <MetricLink variant="compact" label="Cost" metric={run.metrics.estimatedCost} />
                    </div>
                    <section className="strategy-lane__answer">
                      <h3>Saved answer</h3>
                      <p>{run.answer.text || 'The saved run contains an empty answer.'}</p>
                    </section>
                    <section className="strategy-lane__citations">
                      <h3>Citations</h3>
                      {run.answer.citations.length > 0 ? (
                        <div className="citation-stack">
                          {run.answer.citations.map((citation) => (
                            <CitationLink citation={citation} key={citation.id} />
                          ))}
                        </div>
                      ) : (
                        <p className="muted-copy">No citations were recorded.</p>
                      )}
                    </section>
                    <section className="strategy-lane__provenance">
                      <h3>Provenance</h3>
                      <div className="strategy-lane__provenance-links">
                        <ArtifactLink artifact={run.runArtifact} compact />
                        <ArtifactLink artifact={run.configuration.artifact} compact />
                        {strategy ? <ArtifactLink artifact={strategy.artifact} compact /> : null}
                      </div>
                    </section>
                    <section className="strategy-lane__execution">
                      <h3>Execution status</h3>
                      <p>{run.executionStatus}</p>
                    </section>
                  </article>
                );
              })}
            </div>
          </>
        )}
      </div>
    </section>
  );
}
