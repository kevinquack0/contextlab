# ContextLab Story and evidence viewer

This React and Vite application has two layers:

- Story is the default case-study entry point. It uses a compact, checked-in evidence ledger and makes no network request.
- Explore the lab loads the detailed saved-run export only after a lab hash is active.

Neither layer runs an experiment, calls a model, or substitutes sample values when evidence is missing or invalid.

## Local quickstart

```sh
npm ci
npm run dev
```

Open the local URL printed by Vite. The page starts in Story. Use Explore the lab to open the detailed viewer, or add one of these hashes:

- `#comparison`
- `#pipeline`
- `#time`
- `#matrix`
- `#replay`
- `#methods`

Run the complete viewer check before release:

```sh
npm run check
```

The project requires Node.js `20.19.0` or a compatible Node.js `22.12.0` or newer release.

## Evidence contracts

Story imports `src/story/evidence.json`. Each displayed Story metric has a repository-relative source path, exact JSON pointer, file SHA-256, semantic artifact SHA-256 when present, scope, and approval status. `src/story/evidence.ts` rejects malformed hashes, private paths, remote artifact URLs, duplicate IDs, and non-scalar values. Proposed public source links live in `src/story/links.json` so release verification can update them in one place.

The lab runtime URL is fixed to `./contextlab-viewer.v1.json`. Query-string and remote export overrides are unsupported. Source opening and raw-output downloads use only local content-addressed URLs of the form `./artifacts/<sha256>/<file>`.

The small fixture in `tests/fixture.ts` is test-only. Nothing in `src/` imports it, and no fixture is copied into `dist/`.

## Export contract

The canonical TypeScript contract is `src/data/contract.ts`. The strict runtime validator is `src/data/validation.ts`. Root must generate this exact object:

```ts
interface ContextLabViewerExport {
  schemaVersion: 'contextlab.viewer.v1';
  exportId: string;
  generatedAt: string;
  title: string;
  interfaceLanguage: 'en';
  tccLanguage: 'pt-BR';
  exportManifest: ArtifactRef;
  strategies: [StrategyRecord, StrategyRecord, StrategyRecord, StrategyRecord, StrategyRecord];
  questions: QuestionRecord[];
  runs: RunRecord[];
  sourceRuns: SourceRunRecord[];
  temporalEvidenceCases: TemporalEvidenceCase[];
  showcase: {
    retrievalWin: ShowcaseInsight;
    temporalEvidence: ShowcaseInsight;
    executionFailure?: ShowcaseInsight;
  };
  strategyMatrix: { artifact: ArtifactRef; cells: StrategyMatrixCell[] };
  methods: MethodsRecord;
}
```

Every `ArtifactRef` requires `kind`, `label`, repository-relative `path`, a complete `sha256`, a matching local content-addressed `staticUrl`, and `mediaType`. Sealed, protected, gold, grade, and scoring paths are rejected again at runtime. Every displayed measured number is a `MetricValue` with a display string, unit, artifact, exact JSON pointer, and at least one ID in the complete source-run registry. Every answer and pipeline citation carries the full raw source plus a content-addressed exact-section target. Every question names exactly five comparison run IDs covering all five strategies.

Each detailed replay run contains its run record, raw output, public configuration, corpus snapshot, public memory-evidence selection, prompt envelope, sanitized execution facts, answer citations, measured execution metrics, all evidence-pipeline stages, and observable trace spans. The evidence pipeline includes every saved initial candidate, persisted R0 scores, and explicit nulls for unscored fallback candidates. The matrix covers the complete public factorial by real task family, reasoning effort, and memory policy. It reports only completion status, evidence-row counts, context, latency, cost, and trial count. Private grades and evaluation dispositions do not choose rows or metrics. The methods object states the experimental boundaries, NovaLearn's synthetic status, the Portuguese TCC context, the source map, the agent reviews, and Kevin Araujo's role as the sole human reviewer.

## Interaction and accessibility

Normal links, buttons, tabs, and the temporal range control provide keyboard operation. Each layer has a skip link. Trace tabs support arrow, Home, and End keys. Tables use keyboard-scrollable regions on narrow screens. Motion explains entry order, active evidence, and state changes. `prefers-reduced-motion: reduce` disables that motion and smooth scrolling. Multi-column layouts collapse to one column on small screens.
