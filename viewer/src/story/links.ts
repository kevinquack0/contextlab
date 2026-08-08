import rawLinks from './links.json';

interface StoryLink {
  id: string;
  label: string;
  href: string;
  target_path: string | null;
  sha256: string | null;
  release_state: 'proposed';
}

const SHA256 = /^[a-f0-9]{64}$/;
const PUBLIC_REPOSITORY = 'https://github.com/kevinquack0/contextlab';

function parseLinks(value: typeof rawLinks): StoryLink[] {
  if (value.schema_version !== 'contextlab.story-links.v1') {
    throw new Error('Story links have an unsupported schema version.');
  }
  return value.links.map((link) => {
    if (!link.href.startsWith(PUBLIC_REPOSITORY)) {
      throw new Error(`Story link ${link.id} does not use the proposed public repository.`);
    }
    if (link.target_path?.startsWith('/') || link.target_path?.includes('..')) {
      throw new Error(`Story link ${link.id} has an unsafe target path.`);
    }
    if (link.sha256 !== null && !SHA256.test(link.sha256)) {
      throw new Error(`Story link ${link.id} has an invalid SHA-256.`);
    }
    return link as StoryLink;
  });
}

export const storyLinks = parseLinks(rawLinks);
