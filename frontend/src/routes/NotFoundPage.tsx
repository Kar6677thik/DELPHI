import { StatePage } from './StatePage';

export function NotFoundPage() {
  return (
    <StatePage code="404" title="Page not found">
      There is no screen at this address. It may have moved, or the link may be mistyped.
    </StatePage>
  );
}
