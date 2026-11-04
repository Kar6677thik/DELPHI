/**
 * Hand-written stand-ins for the auth contract (docs/16 §2, docs/19 §3) until #6 adds the
 * endpoints and their schemas reach openapi.json. Replace each with its generated type then
 * (docs/tech-debt.md TD-5).
 */

export type Role = 'VIEWER' | 'INVESTIGATOR' | 'FDE_ENGINEER' | 'ADMIN';

export type Permission = 'view' | 'demo:drive' | 'case:write' | 'chase:write' | 'fde' | 'admin';

/** `GET /auth/me`. */
export interface Me {
  id: string;
  username: string;
  display_name: string;
  role: Role;
  permissions: Permission[];
}

/** `POST /auth/login`. */
export interface LoginRequest {
  username: string;
  password: string;
}
