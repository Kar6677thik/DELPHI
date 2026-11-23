import * as DropdownMenu from '@radix-ui/react-dropdown-menu';
import { useNavigate } from 'react-router';

import { useLogout } from '../api/auth';
import type { Me } from '../api/types';

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
      <DropdownMenu.Trigger className="-mx-1 rounded px-1 py-1 text-left text-[13px] text-soft hover:text-ink">
        {me.display_name} <span className="text-muted">({me.role})</span>
      </DropdownMenu.Trigger>
      <DropdownMenu.Portal>
        <DropdownMenu.Content
          align="start"
          side="top"
          sideOffset={4}
          className="min-w-44 rounded-md border border-control bg-raised p-1 text-[13px] shadow-[0_12px_28px_rgba(0,0,0,.35)]"
        >
          <DropdownMenu.Item
            onSelect={signOut}
            disabled={logout.isPending}
            className="cursor-pointer rounded px-2 py-1.5 outline-none data-[highlighted]:bg-control"
          >
            Sign out
          </DropdownMenu.Item>
        </DropdownMenu.Content>
      </DropdownMenu.Portal>
    </DropdownMenu.Root>
  );
}
