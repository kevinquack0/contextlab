import { Accordion, AccordionItem, Tag } from '@carbon/react';

import type { ContextLabViewerExport } from '../data/contract';
import { ArtifactLink } from '../components/ProvenanceLink';
import { ViewHeader } from '../components/ViewPrimitives';

export default function MethodsSources({ data }: { data: ContextLabViewerExport }) {
  const { methods } = data;

  return (
    <section aria-labelledby="methods-heading" className="methods-view view-stack">
      <ViewHeader
        description="Read the experimental contract, review boundaries, limitations, and the artifact map behind this export."
        title="Methods and sources"
      />
      <section className="methods-intro">
        <div>
          <h2 id="methods-heading">Evidence before interpretation</h2>
          <p>{methods.novaLearnSyntheticStatement}</p>
        </div>
        <ArtifactLink artifact={methods.experimentalContract} />
      </section>
      <section className="methods-boundaries">
        <article>
          <h2>v1 and v2 boundary</h2>
          <p>{methods.v1V2Boundary}</p>
        </article>
        <article>
          <h2>Review boundary</h2>
          <p>{methods.reviewBoundary}</p>
        </article>
        <article>
          <h2>Sealed-data boundary</h2>
          <p>{methods.sealedDataBoundary}</p>
        </article>
      </section>
      <section className="review-panel">
        <header>
          <h2>Independent review panel</h2>
          <p>The export names each reviewer and links to the saved review protocol.</p>
        </header>
        <div className="reviewer-grid">
          {methods.reviewers.aiJudges.map((reviewer) => (
            <article key={reviewer.id}>
              <Tag size="sm" type="blue">AI judge</Tag>
              <h3>{reviewer.name}</h3>
              <p>{reviewer.modelId}, {reviewer.reasoningEffort} reasoning</p>
              <p>{reviewer.invocation}</p>
              <ArtifactLink artifact={reviewer.artifact} />
            </article>
          ))}
          <article>
            <Tag size="sm" type="cool-gray">Sole human reviewer</Tag>
            <h3>{methods.reviewers.human.name}</h3>
            <p>{methods.reviewers.human.invocation}</p>
            <ArtifactLink artifact={methods.reviewers.human.artifact} />
          </article>
        </div>
      </section>
      <section className="limitations-panel">
        <h2>Known limitations</h2>
        <ul>
          {methods.limitations.map((limitation) => (
            <li key={limitation}>{limitation}</li>
          ))}
        </ul>
      </section>
      <section>
        <h2>AI-Brain and primary-source map</h2>
        <Accordion align="start" size="lg">
          {methods.sourceMap.map((group) => (
            <AccordionItem key={group.label} title={group.label}>
              <p>{group.description}</p>
              <div className="source-map-links">
                {group.artifacts.map((artifact) => (
                  <ArtifactLink artifact={artifact} key={`${artifact.path}-${artifact.sha256}`} />
                ))}
              </div>
            </AccordionItem>
          ))}
        </Accordion>
      </section>
      <section className="portuguese-summary" lang="pt-BR">
        <h2>Explicação para o TCC</h2>
        <p>{methods.portugueseSummary}</p>
      </section>
    </section>
  );
}
