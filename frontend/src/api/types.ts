import type { components } from './schema';

/**
 * Names for the generated API types (docs/16, docs/18 §1): they come from backend/openapi.json
 * through `pnpm gen:api`, never by hand.
 */
type Schemas = components['schemas'];

/** `GET /auth/me`. */
export type Me = Schemas['MeView'];
/** `POST /auth/login`. */
export type LoginRequest = Schemas['LoginRequest'];
export type Role = Schemas['Role'];
export type Permission = Schemas['Permission'];
/** `GET /cases/{id}` and the rows of `GET /cases` (the M4 case stub, #97). */
export type CaseView = Schemas['CaseView'];
/** `GET /cases`: one keyset page of the case queue. */
export type CasePage = Schemas['CasePage'];
