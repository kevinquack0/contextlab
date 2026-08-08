import rawEvidence from './evidence.json';

export type EvidenceValue = string | number | boolean | null;

export interface StoryEvidenceEntry {
  id: string;
  value: EvidenceValue;
  status: string;
  scope: string;
  source_path: string;
  source_file_sha256: string;
  source_artifact_sha256: string | null;
  json_pointer: string;
  public_url: string | null;
}

const SHA256 = /^[a-f0-9]{64}$/;
const PRIVATE_PATH = /^(?:\/Users\/|\/Volumes\/|[A-Za-z]:\\)/;

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

function parseEntry(value: unknown, index: number): StoryEvidenceEntry {
  if (!isRecord(value)) throw new Error(`Story evidence entry ${index} must be an object.`);

  for (const field of ['id', 'status', 'scope', 'source_path', 'source_file_sha256', 'json_pointer']) {
    if (typeof value[field] !== 'string' || value[field].length === 0) {
      throw new Error(`Story evidence entry ${index} has an invalid ${field}.`);
    }
  }
  if (!SHA256.test(value.source_file_sha256 as string)) {
    throw new Error(`Story evidence entry ${index} has an invalid source_file_sha256.`);
  }
  if (value.source_artifact_sha256 !== null && !SHA256.test(String(value.source_artifact_sha256))) {
    throw new Error(`Story evidence entry ${index} has an invalid source_artifact_sha256.`);
  }
  if (!String(value.json_pointer).startsWith('/')) {
    throw new Error(`Story evidence entry ${index} has an invalid JSON pointer.`);
  }
  if (PRIVATE_PATH.test(String(value.source_path))) {
    throw new Error(`Story evidence entry ${index} exposes a private path.`);
  }
  if (typeof value.public_url !== 'string' && value.public_url !== null) {
    throw new Error(`Story evidence entry ${index} has an invalid public_url.`);
  }
  if (typeof value.public_url === 'string' && !value.public_url.startsWith('./artifacts/')) {
    throw new Error(`Story evidence entry ${index} has a non-local public_url.`);
  }
  if (
    value.value !== null &&
    typeof value.value !== 'string' &&
    typeof value.value !== 'number' &&
    typeof value.value !== 'boolean'
  ) {
    throw new Error(`Story evidence entry ${index} has a non-scalar value.`);
  }

  return value as unknown as StoryEvidenceEntry;
}

function parseEvidence(value: unknown): StoryEvidenceEntry[] {
  if (!isRecord(value) || value.schema_version !== 'contextlab.story-evidence.v1') {
    throw new Error('Story evidence has an unsupported schema version.');
  }
  if (!Array.isArray(value.metrics)) throw new Error('Story evidence metrics must be an array.');
  const entries = value.metrics.map(parseEntry);
  const ids = new Set<string>();
  for (const entry of entries) {
    if (ids.has(entry.id)) throw new Error(`Story evidence id ${entry.id} is duplicated.`);
    ids.add(entry.id);
  }
  return entries;
}

export const storyEvidence = parseEvidence(rawEvidence);
const evidenceById = new Map(storyEvidence.map((entry) => [entry.id, entry]));

export function getEvidence(id: string): StoryEvidenceEntry {
  const entry = evidenceById.get(id);
  if (!entry) throw new Error(`Story evidence id ${id} is missing.`);
  return entry;
}

export function formatEvidenceValue(entry: StoryEvidenceEntry): string {
  if (entry.value === null) return 'none';
  if (typeof entry.value === 'number') return new Intl.NumberFormat('en-US').format(entry.value);
  if (typeof entry.value === 'boolean') return entry.value ? 'true' : 'false';
  return entry.value;
}

export function publicArtifactHref(entry: StoryEvidenceEntry): string | null {
  return entry.public_url;
}
