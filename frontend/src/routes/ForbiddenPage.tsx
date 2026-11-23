import { StatePage } from './StatePage';

export function ForbiddenPage() {
  return (
    <StatePage code="403" title="No access">
      Your role does not include this screen. An admin can change your role on the Admin screen.
    </StatePage>
  );
}
