import { DropdownMenu } from 'radix-ui';
import { useNavigate } from 'react-router';

import { useLogout } from '../api/auth';
import type { Me } from '../api/pending-contracts';

export function UserMenu({ me }: { me: Me }) {
  const logout = useLogout();
  const navigate = useNavigate();

  const signOut = () => {
    logout.mutate(undefined, {
      onSettled: () => {
        void navigate('/login', { replace: true });
      },
    });
  };

  return (
    <DropdownMenu.Root>
      <DropdownMenu.Trigger className="rounded-md px-3 py-1.5 text-sm hover:bg-slate-100 focus-visible:outline-2 focus-visible:outline-slate-900">
        {me.display_name} <span className="text-slate-600">({me.role})</span>
      </DropdownMenu.Trigger>
      <DropdownMenu.Portal>
        <DropdownMenu.Content
          align="end"
          sideOffset={4}
          className="min-w-40 rounded-md border border-slate-200 bg-white p-1 text-sm shadow-md"
        >
          <DropdownMenu.Item
            onSelect={signOut}
            disabled={logout.isPending}
            className="cursor-pointer rounded px-2 py-1.5 outline-none data-[highlighted]:bg-slate-100"
          >
            Sign out
          </DropdownMenu.Item>
        </DropdownMenu.Content>
      </DropdownMenu.Portal>
    </DropdownMenu.Root>
  );
}
