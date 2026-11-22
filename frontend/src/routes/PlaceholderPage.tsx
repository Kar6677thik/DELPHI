import { StatePage } from './StatePage';

/** What brings each unbuilt screen (docs/25), in words a visitor understands. */
const ARRIVES: Readonly<Record<string, string>> = {
  '/campaigns': 'campaigns, which group cases that share a fraudster',
  '/campaigns/:id': 'campaigns, which group cases that share a fraudster',
  '/chase/:id': 'Jatayu, the chase that follows stolen money through mule accounts',
  '/scout': 'the Typology Scout, which proposes new fraud patterns from unexplained cases',
  '/scout/drafts/:id':
    'the Typology Scout, which proposes new fraud patterns from unexplained cases',
  '/kb/questions': 'the question bank review, for reviewing translated questions',
  '/learning': 'the learning loop, which updates the knowledge base from outcomes',
  '/evaluation': 'the evaluation, which compares DELPHI with the bank’s rules on the same payments',
  '/cases/:id/dd-pack': 'the case DD pack page',
};

/** A screen from docs/18 §2 that is routed and permission-gated but not built yet. */
export function PlaceholderPage({ title, path }: { title: string; path?: string }) {
  const arrives = path ? ARRIVES[path] : undefined;
  return (
    <StatePage code="SOON" title={title}>
      This screen is still being built
      {arrives ? <>. It arrives with {arrives}.</> : '.'}
    </StatePage>
  );
}
